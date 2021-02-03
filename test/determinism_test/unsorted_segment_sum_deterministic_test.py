# Copyright 2020 NVIDIA Corporation. All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Determinism tests for segment reduction ops."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import warnings
sys.path.insert(0, '..')

import numpy as np
import tensorflow as tf

from fwd9m import tensorflow as fwd9m_tensorflow
from segment_reduction_helper import SegmentReductionHelper
from tensorflow.python.client import session
from tensorflow.python.eager import backprop
from tensorflow.python.eager import context
from tensorflow.python.framework import dtypes as dtypes_lib
from tensorflow.python.framework import ops
from tensorflow.python.framework import test_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import gradients_impl
from tensorflow.python.ops import math_ops
from tensorflow.python.platform import test
import utils as tests_utils

# The deterministic tests in the following class were originally copied from
# test_patch_segment_sum.py.

# NOTE:
# 1. Op `segment_sum` is expected to be deterministic on CPU and it indeed
#    behaves like that if forcely pinned to CPU with tf.device('/device:CPU:0').
#    What is not fully understood is that without explicitly pinned to CPU but
#    with `Session(use_gpu=False)`, it seems to run on GPU still and produces
#    nondeterminism. But by setting `log_device_placement=True` configuration,
#    command line outputs indicate the op is pinned to CPU. ?? Should it be kept
# 2. To capture nondeterminism, random input data is necessary.
# 3. GPU-nondeterminism of float64 cannot be fixed by this patch, so it's not
#    tested.


class UnsortedSegmentSumDeterministicTest(SegmentReductionHelper):

  def __init__(self, methodName='runTest'):
    # Each item is np_op1, np_op2, tf_op, initial_value functor
    self.ops_list = [(np.add, None,
                      math_ops.unsorted_segment_sum, lambda t: 0),
                     (np.add, None,
                      tf.math.unsorted_segment_sum, lambda t: 0)]

    # A subset of ops has been enabled for complex numbers
    self.complex_ops_list = [(np.add, None,
                              math_ops.unsorted_segment_sum, lambda t: 0),
                             (np.add, None,
                              tf.math.unsorted_segment_sum, lambda t: 0)]

    self.differentiable_dtypes = [dtypes_lib.float16, dtypes_lib.float32]

    self.all_dtypes = (self.differentiable_dtypes +
                       [dtypes_lib.complex64, dtypes_lib.bfloat16])
    self.repeat_count = 5
    super(
        UnsortedSegmentSumDeterministicTest, self).__init__(
            methodName=methodName)

  def _testBackwardCase(self, dtype, indices, num_segments, op_binding, shape):
    numpy_seed = 123
    _, _, tf_op, _ = op_binding

    input_val = self._randomDataOp(shape, dtype, seed=None)

    if context.executing_eagerly():
      def op_gradients(local_seed):
        with backprop.GradientTape() as tape:
          tape.watch(input_val)
          op_output = tf_op(input_val, indices, num_segments)
          upstream_gradients = self._randomDataOp(op_output.shape,
                                                  dtype, local_seed)
          gradient_injector_output = op_output * upstream_gradients
        return tape.gradient(gradient_injector_output, input_val)

      for i in range(self.repeat_count):
        local_seed = numpy_seed + i # select different upstream gradients
        result_a = op_gradients(local_seed)
        result_b = op_gradients(local_seed)
        self.assertAllEqual(result_a, result_b)

    else:
      op_output = tf_op(input_val, indices, num_segments)
      output_shape = op_output.shape
      upstream_gradients = array_ops.placeholder(dtype, shape=output_shape,
                                                 name='upstream_gradients')
      gradient_injector_output = op_output * upstream_gradients
      op_gradients = gradients_impl.gradients(
            gradient_injector_output,
            input_val,
            grad_ys=None,
            colocate_gradients_with_ops=True)[0]

      for i in range(self.repeat_count):
        feed_dict = {upstream_gradients:np.random.random(output_shape)}
        result_a = op_gradients.eval(feed_dict=feed_dict)
        result_b = op_gradients.eval(feed_dict=feed_dict)
        self.assertAllEqual(result_a, result_b)


  # The backward operation is not known or expected to introduce nondeterminism
  # but we're testing it for completeness.
  @test_util.run_in_graph_and_eager_modes
  def testBackward(self):
    num_cols = 2
    num_rows = 64
    num_segments = 64
    segment_size = num_cols * num_rows
    indices_flat = np.random.randint(low=-1, high=num_segments,
                                     size=(segment_size,))

    with tests_utils.force_gpu_session(self):
      for dtype in self.differentiable_dtypes:
        for indices in indices_flat, indices_flat.reshape(num_rows, num_cols):
          ops_list = self.complex_ops_list if dtype.is_complex \
              else self.ops_list
          for op_binding in ops_list:
            shape = indices.shape + (num_cols,)
            self._testBackwardCase(dtype, indices, num_segments,
                                   op_binding, shape)

  @test_util.run_in_graph_and_eager_modes
  def testForward(self):
    num_cols = 2
    num_rows = 64
    num_segments = 64
    segment_size = num_cols * num_rows
    indices_flat = np.random.randint(low=-1, high=num_segments,
                                     size=(segment_size,))
    with tests_utils.force_gpu_session(self):
      for dtype in self.all_dtypes:
        for indices in indices_flat, indices_flat.reshape(num_rows, num_cols):
          shape = indices.shape + (num_cols,)
          ops_list = self.complex_ops_list if dtype.is_complex else self.ops_list
          x, _  = self._random_input(shape, dtype=dtype)

          for _, _, tf_op, _ in ops_list:
            for _ in range(self.repeat_count):
              result_a = self.evaluate(tf_op(x, indices, num_segments))
              result_b = self.evaluate(tf_op(x, indices, num_segments))
              self.assertAllEqual(result_a, result_b)


if __name__ == "__main__":
  os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2' # Simplifies logging
  fwd9m_tensorflow.enable_determinism()
  test.main()
