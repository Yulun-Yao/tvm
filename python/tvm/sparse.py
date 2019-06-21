from __future__ import absolute_import as _abs
from ._ffi.node import NodeBase, NodeGeneric, register_node, convert_to_node
from . import _api_internal


@register_node
class SparseFormat(NodeBase):
    Dense = 0
    Sparse = 1

def dense_format(n):
    return _api_internal._SparseFormat([SparseFormat.Dense for i in range(n)])

# def decl_sparse_fmt(stypes):
#     return _api_internal._SparseFormat(stypes)
