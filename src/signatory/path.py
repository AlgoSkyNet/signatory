# Copyright 2019 Patrick Kidger. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================
"""Provides the Path class, a high-level object capable of giving signatures over intervals."""

import bisect
import torch
from torch import autograd

from . import backend
from . import signature_module as smodule
from . import _impl

# noinspection PyUnreachableCode
if False:
    from typing import List, Union


class _TensorAlgebraMult(autograd.Function):
    @staticmethod
    def forward(ctx, arg1, arg2, input_channels, depth):
        ctx.save_for_backward(arg1, arg2)
        ctx.input_channels = input_channels
        ctx.depth = depth
        return _impl.tensor_algebra_mult_forward(arg1, arg2, input_channels, depth)

    @staticmethod
    def backward(ctx, grad):
        arg1, arg2 = ctx.saved_tensors
        grad_arg1, grad_arg2 = _impl.tensor_algebra_mult_backward(grad, arg1, arg2, ctx.input_channels, ctx.depth)
        return grad_arg1, grad_arg2, None, None


class Path(object):
    """Calculates signatures on intervals of an input path.

    By doing some precomputation, it can rapidly calculate the signature of the input path over any interval.

    Arguments:
        path (torch.Tensor): As :func:`signatory.signature`.

        depth (int): As :func:`signatory.signature`.

        basepoint (bool or torch.Tensor, optional): As :func:`signatory.signature`.
    """
    def __init__(self, path, depth, basepoint=False):
        # type: (torch.Tensor, int, Union[bool, torch.Tensor]) -> None
        self._depth = depth

        self._signature = []
        self._inverse_signature = []

        self._path = []

        basepoint, basepoint_value = backend.interpret_basepoint(basepoint, path)
        if basepoint:
            self._path.append(basepoint_value)

        self._length = 0
        self._signature_length = 0
        self._signature_lengths = []

        self._update(path, basepoint_value, None, None)

        self._batch_sizes = self.shape[:-2]
        self._signature_channels = self.signature_size(-1)
        self._channels = self.size(-1)

    def update(self, path):
        # type: (torch.Tensor) -> None
        """Concatenates the given path onto the path already stored.

        Arguments:
            path (torch.Tensor): The path to concatenate on. As :func:`signatory.signature`.
        """
        if path.shape[:-2] != self._batch_sizes:
            raise ValueError("Cannot append a path with different batch dimensions to what has already been used.")
        if path.size(-1) != self._channels:
            raise ValueError("Cannot append a path with different number of channels to what has already been used.")
        basepoint = self._path[-1][:, -1, :]
        initial = self._signature[-1][:, -1, :]
        inverse_initial = self._inverse_signature[-1][:, -1, :]
        self._update(path, basepoint, initial, inverse_initial)

    def _update(self, path, basepoint, initial, inverse_initial):
        signature = smodule.signature(path, self._depth, stream=True, basepoint=basepoint, initial=initial)
        inverse_signature = smodule.signature(path, self._depth, stream=True, basepoint=basepoint, inverse=True,
                                              initial=inverse_initial)
        self._signature.append(signature)
        self._inverse_signature.append(inverse_signature)

        self._path.append(path)

        self._length += path.size(-2)
        self._signature_length += signature.size(-2)
        self._signature_lengths.append(self._signature_length)

        self._shape = list(path.shape)
        self._shape[-2] = self._length
        self._signature_shape = list(signature.shape)
        self._signature_shape[-2] = self._signature_length

    def signature(self, start=None, end=None):
        # type: (Union[int, None], Union[int, None]) -> torch.Tensor
        """Returns the signature on a particular interval.

        Arguments:
            start (int or None, optional): Defaults to the start of the path. The start point of the interval to 
                calculate the signature on.

            end (int or None, optional): Defaults to the end of the path. The end point of the interval to calcluate 
                the signature on.

        Returns:
            The signature on the interval :attr:`[start, end]`. That is, let :attr:`p` be the input :attr:`path`
            with basepoint prepended. Then this function is equivalent to
            :attr:`signatory.signature(p[start:end], depth)`.
        """

        old_start = start
        old_end = end

        length = self._signature_length + 1
        if start is None:
            start = 0
        if end is None:
            end = length
        # We're duplicating slicing behaviour, which means to accept values even beyond the normal indexing range
        if start < -length:
            start = -length
        elif start > length:
            start = length
        if end < -length:
            end = -length
        elif end > length:
            end = length
        # Accept negative indices
        if start < 0:
            start += length
        if end < 0:
            end += length

        if end - start < 2:
            raise ValueError("start={}, end={} is interpreted as start={}, end={} for path of length {}, which "
                             "does not describe a valid interval.".format(old_start, old_end, start, end, self._length))

        start -= 1
        end -= 2

        index_end = bisect.bisect_right(self._signature_lengths, end)
        adjusted_end = end - self._signature_lengths[index_end]

        if start == -1:
            return self._signature[index_end][:, adjusted_end, :]

        index_start = bisect.bisect_right(self._signature_lengths, start)
        adjusted_start = start - self._signature_lengths[index_start]

        rev = self._inverse_signature[index_start][:, adjusted_start, :]
        sig = self._signature[index_end][:, adjusted_end, :]

        return _TensorAlgebraMult.apply(rev, sig, self._channels, self.depth)

    @property
    def path(self):
        # type: () -> List[torch.Tensor]
        """The paths that this Path was created with."""
        return self._path

    @property
    def depth(self):
        # type: () -> int
        """The depth that Path has calculated the signature to."""
        return self._depth

    def size(self, index=None):
        # type: (Union[int, None]) -> Union[int, torch.Size]
        """The size of the input path. As :meth:`torch.Tensor.size`.

        Arguments:
            index (int or None, optional): As :meth:`torch.Tensor.size`.

        Returns:
            As :meth:`torch.Tensor.size`.
        """
        if index is None:
            return self.shape
        else:
            return self._shape[index]

    @property
    def shape(self):
        # type: () -> torch.Size
        """The shape of the input path. As :meth:`torch.Tensor.shape`."""
        return torch.Size(self._shape)

    def signature_size(self, index=None):
        # type: (Union[int, None]) -> Union[int, torch.Size]
        """The size of the signature of the path. As :meth:`torch.Tensor.size`.

        Arguments:
            index (int or None, optional): As :meth:`torch.Tensor.size`.

        Returns:
            As :meth:`torch.Tensor.size`.
        """
        if index is None:
            return self.signature_shape
        else:
            return self._signature_shape[index]

    @property
    def signature_shape(self):
        # type: () -> torch.Size
        """The shape of the signature of the path. As :meth:`torch.Tensor.shape`."""
        return torch.Size(self._signature_shape)

    # Method not property for consistency with signatory.signature_channels
    def signature_channels(self):
        # type: () -> int
        """The number of signature channels; as :func:`signatory.signature_channels`."""
        return self._signature_channels
