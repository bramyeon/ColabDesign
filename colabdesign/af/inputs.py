import jax
import jax.numpy as jnp
import numpy as np

from colabdesign.shared.utils import copy_dict
from colabdesign.shared.model import soft_seq
from colabdesign.shared.parsers import parse_a3m
from colabdesign.af.alphafold.common import residue_constants
from colabdesign.af.alphafold.model import model, config
from colabdesign.af.prep import prep_pdb

############################################################################
# AF_INPUTS - functions for modifying inputs before passing to alphafold
############################################################################
class _af_inputs:

  def set_seq(self, seq=None, mode=None, **kwargs):
    assert self._args["optimize_seq"] == True
    self._set_seq(seq=seq, mode=mode, **kwargs)

  def set_msa(self, msa=None, deletion_matrix=None, a3m_filename=None):
    ''' set msa '''
    assert self._args["optimize_seq"] == False

    if a3m_filename is not None:
      msa, deletion_matrix = parse_a3m(a3m_filename=a3m_filename)

    if msa is None:
      msa = np.zeros((self._num, self._len),int)

    if msa.ndim == 1:
      msa = msa[None]

    if deletion_matrix is None:
      deletion_matrix = np.zeros(msa.shape)

    if self.protocol == "binder" and msa.shape[1] == self._binder_len:
      # add target sequence
      msa = np.pad(msa,[[0,0],[self._target_len,0]])
      deletion_matrix = np.pad(deletion_matrix,[[0,0],[self._target_len,0]])
      msa[0,:self._target_len] = self._wt_aatype[:self._target_len]
      msa[1:,:self._target_len,-1] = 1
    
    self._inputs["msa"] = msa

  def set_template(self, pdb_filename=None, chain="A", batch=None, n=0, ignore_missing=True):
    '''set template'''
    assert self._args["use_templates"] == True

    if batch is None:
      batch = prep_pdb(pdb_filename, chain=chain, ignore_missing=ignore_missing)["batch"]
      assert batch["aatype"].shape[0] == self._inputs["template_aatype"].shape[1]

    if n == 0 and self._arg["use_batch_as_template"]:
      self._inputs["batch"].update(batch)
    else:
      self._inputs["template_mask"][n] = 1
      for k in ["aatype","all_atom_mask","all_atom_positions"]:
        self._inputs[f"template_{k}"][n] = batch[k]

  def _update_seq(self, params, inputs, aux, key):
    '''get sequence features'''

    opt = inputs["opt"]
    if self._args["optimize_seq"]:
      L = params["seq"].shape[1]      
      seq = soft_seq(params["seq"], inputs["bias"][:L], opt, key, num_seq=self._num,
                     shuffle_first=self._args["shuffle_first"])
      msa = seq["pseudo"]

      # pad msa to 22 amino acids (20, unk, gap)
      msa = jnp.pad(msa,[[0,0],[0,0],[0,22-msa.shape[-1]]])
      
      # fix positions
      wt_seq = jax.nn.one_hot(inputs["wt_aatype"][:L],22)
      msa = jnp.where(inputs["fix_pos"][:L,None],wt_seq,msa)
      
      # expand copies for homooligomers
      if self._args["copies"] > 1:
        f = dict(copies=self._args["copies"], block_diag=self._args["block_diag"])
        msa = expand_copies(msa, jnp.eye(22)[-1], **f)

      # define features
      inputs.update({
        "msa":msa,
        "target_feat":msa[0,:,:20],
        "aatype":msa[0].argmax(-1),
        "deletion_matrix":jnp.zeros(msa.shape[:2]),
        "msa_mask":jnp.ones(msa.shape[:2])
      }) 
    
    else:
      f = dict(copies=self._args["copies"], block_diag=self._args["block_diag"])
      msa = expand_copies(inputs["msa"], 21, **f),
      inputs.update({
        "msa":msa,
        "target_feat":jax.nn.one_hot(msa,20),
        "aatype":msa[0],
        "deletion_matrix":expand_copies(inputs["deletion_matrix"], **f),
        "msa_mask":jnp.ones(msa.shape[:2])
      })
    
    return seq

  def _update_template(self, inputs):
    ''''dynamically update template features''' 

    opt = inputs["opt"]
    # gather features
    if self._args["use_batch_as_template"]:
      batch = inputs["batch"]
      (T,L) = (1,batch["aatype"].shape[0])

      # define template features
      template_feats = {"template_aatype":batch["aatype"]}

      if "dgram" in batch:
        # use dgram from batch if provided
        template_feats.update({"template_dgram":batch["dgram"]})
        nT,nL = inputs["template_aatype"].shape
        inputs["template_dgram"] = jnp.zeros((nT,nL,nL,39))
        
      if "all_atom_positions" in batch:
        # use coordinates from batch if provided
        template_feats.update({"template_all_atom_positions": batch["all_atom_positions"],
                               "template_all_atom_mask":      batch["all_atom_mask"]})
      # enable templates
      inputs["template_mask"] = inputs["template_mask"].at[:T].set(1)
      
      # inject template features
      for k,v in template_feats.items():
        inputs[k] = inputs[k].at[:T].set(v) 
    
    else:
      (T,L) = inputs["template_aatype"].shape
      
    # decide which position to remove sequence and/or sidechains
    opt_T  = opt["template"]
    rm     = jnp.broadcast_to(inputs.get("rm_template",opt_T["rm"]),L)
    rm_seq = jnp.where(rm,True,jnp.broadcast_to(inputs.get("rm_template_seq",opt_T["rm_seq"]),L))
    rm_sc  = jnp.where(rm_seq,True,jnp.broadcast_to(inputs.get("rm_template_sc",opt_T["rm_sc"]),L))

    # remove sidechains (mask anything beyond CB)
    k = "template_aatype"
    inputs[k] = jnp.where(rm_seq[:,None],21,inputs[k])
    jnp.where(rm_seq,21,)
    k = "template_all_atom_mask"
    inputs[k] = inputs[k].at[:T,:,5:].set(jnp.where(rm_sc[:,None],0,inputs[k][:T,:,5:]))
    inputs[k] = jnp.where(rm[:,None],0,inputs[k])

def np_one_hot(x, alphabet):
  return np.pad(np.eye(alphabet),[[0,1],[0,0]])[x]

def expand_copies(x, x_default=0, copies=1, block_diag=False, use_jax=True):
  _np = jnp if use_jax else np
  # decide on new shape
  if x.ndim == 1:
    N,L,A = (1,x.shape[0],1)
    new_shape = (L*copies,)
    x = x[None,:,None]
    block_diag = False
  elif x.ndim == 2:
    N,L,A = x.shape + (1,)
    new_shape = (N*copies+1,L*copies) if block_diag else (N,L*copies)
    x = x[:,:,None]
  else:
    N,L,A = x.shape
    new_shape = (N*copies+1,L*copies,A) if block_diag else (N,L*copies,A)
  
  x = jnp.tile(x,[1,copies,1])
  if block_diag:
    x_ = x.reshape(N,copies,L,A)
    x_diag = _np.zeros((N,copies,copies,L,A),dtype=x.dtype)
    i = _np.arange(copies)
    if use_jax:
      x_diag = x_diag.at[...,:].set(x_default).at[:,i,i].set(x_)
    else:
      x_diag[...,:] = x_default
      x_diag[:,i,i] = x_
    x_diag = x_diag.swapaxes(0,1).reshape(N*copies,copies*L,A)
    x = _np.concatenate([x[:1],x_diag],0)
  return x.reshape(new_shape)