"""
Auto-tuning a convolutional network for ARM CPU
====================================================
**Author**: `Lianmin Zheng <https://https://github.com/merrymercy>`_

Auto-tuning for a specific ARM device is critical for getting the best
performance. This is a tutorial about how to tune a whole convolutional
network.

The operator implementation for ARM CPU in TVM is written in template form.
It has many tunable knobs (tile factor, vectorization, unrolling, etc).
We will do tuning for all convolution and depthwise convolution operators
in the neural network. After the tuning, we can get a log file which stores
the best knob values for all required operators. When the tvm compiler compiles
these operators, it will query this log file to get the best knob values.

We also released pre-tuned parameters for some arm devices. You can go to
`ARM CPU Benchmark <https://github.com/dmlc/tvm/wiki/Benchmark#arm-cpu>`_
to see the results.
"""

######################################################################
# Install dependencies
# ----------------------------------------
# To use autotvm package in tvm, we need to install some extra dependencies.
# (change "3" to "2" if you use python2):
#
# .. code-block:: bash
#
#   pip3 install --user psutil xgboost tornado
#
# To make tvm run faster in tuning, it is recommended to use cython
# as FFI of tvm. In the root directory of tvm, execute
# (change "3" to "2" if you use python2):
#
# .. code-block:: bash
#
#   pip3 install --user cython
#   sudo make cython3
#
# Now return to python code. Import packages.

import os

import numpy as np

import nnvm.testing
import nnvm.compiler
import tvm
from tvm import autotvm
from tvm.autotvm.tuner import XGBTuner, GATuner, RandomTuner, GridSearchTuner
from tvm.contrib.util import tempdir
import tvm.contrib.graph_runtime as runtime

#################################################################
# Define network
# --------------
# First we need to define the network in nnvm symbol API.
# We can load some pre-defined network from :code:`nnvm.testing`.
# We can also load models from MXNet, ONNX and TensorFlow (see NNVM
# tutorials :ref:`tutorial-nnvm` for more details).

def get_network(name, batch_size):
    """Get the symbol definition and random weight of a network"""
    shape = {"data": (batch_size, 3, 224, 224)}
    output_shape = (batch_size, 1000)

    if name =='resnet-18':
        net, params = nnvm.testing.resnet.get_workload(num_layers=18, batch_size=batch_size)
    elif name =='mobilenet':
        net, params = nnvm.testing.mobilenet.get_workload(batch_size=batch_size)
    elif name =='squeezenet v1.1':
        net, params = nnvm.testing.squeezenet.get_workload(batch_size=batch_size, version='1.1')
    elif name =='vgg-16':
        net, params = nnvm.testing.vgg.get_workload(num_layers=16, batch_size=batch_size)
    elif name =='custom':
        # an example for custom network
        from nnvm.testing import utils
        net = nnvm.sym.Variable('data')
        net = nnvm.sym.conv2d(net, channels=4, kernel_size=(3,3), padding=(1,1))
        net = nnvm.sym.flatten(net)
        net = nnvm.sym.dense(net, units=1000)
        net, params = utils.create_workload(net, batch_size, (3, 224, 224))
    elif name == 'mxnet':
        # an example for mxnet model
        from mxnet.gluon.model_zoo.vision import get_model
        block = get_model('resnet18_v1', pretrained=True)
        net, params = nnvm.frontend.from_mxnet(block)
        net = nnvm.sym.softmax(net)
    else:
        raise ValueError("Unsupported network: " + name)

    return net, params, shape, output_shape

#################################################################
# Start RPC Tracker
# -----------------
# TVM uses RPC session to communicate with ARM boards.
# During tuning, the tuner will send the generated code to the board and
# measure the speed of code on the board.
#
# To scale up the tuning, TVM uses RPC Tracker to manage distributed devices.
# The RPC Tracker is a centralized master node. We can register all devices to
# the tracker. For example, if we have 10 phones, we can register all of them
# to the tracker, then we can run 10 measurements in parallel, which accelerates
# the tuning process.
#
# To start an RPC tracker, run this command in the host machine. The tracker is
# required during the whole tuning process, so we need to open a new terminal for
# this command:
#
# .. code-block:: bash
#
#   python -m tvm.exec.rpc_tracker --host=0.0.0.0 --port=9190
#
# The expected output is
#
# .. code-block:: bash
#
#   INFO:RPCTracker:bind to 0.0.0.0:9190

#################################################################
# Register devices to RPC Tracker
# -----------------------------------
# Now we can register our devices to the tracker. The first step is to
# build tvm runtime for the ARM devices.
#
# * For Linux:
#   Follow this section :ref:`build-tvm-runtime-on-device` to build
#   tvm runtime on the device. Then register the device to tracker by
#
#   .. code-block:: bash
#
#     python -m tvm.exec.rpc_server --tracker=[HOST_IP]:9190 --key=rk3399
#
#   (replace :code:`[HOST_IP]` with the IP address of your host machine)
#
# * For Android:
#   Follow this `readme page <https://github.com/dmlc/tvm/tree/master/apps/android_rpc>`_ to
#   install tvm rpc apk on the android device. Make sure you can pass the android rpc test.
#
# After registering devices, we can confirm it by querying rpc_tracker
#
# .. code-block:: bash
#
#   python -m tvm.exec.query_rpc_tracker --host=0.0.0.0 --port=9190
#
# For example, if we have 2 Huawei mate10 pro, 11 Raspberry Pi 3B and 2 rk3399,
# the output can be
#
# .. code-block:: bash
#
#    Queue Status
#    ----------------------------
#    key          free    pending
#    ----------------------------
#    mate10pro    2       0
#    rk3399       2       0
#    rpi3b        11      0
#    ----------------------------

###########################################
# Set Tuning Options
# ------------------
# Before tuning, we should do some configurations. Here I use an RK3399 board
# in our environment as example. In your setting, you should modify the target
# and device_key accordingly.

# Replace "aarch64-linux-gnu" with the correct target of your board.
# This target is used for cross compilation. You can query it by :code:`gcc -v` on your device.
target = tvm.target.create('llvm -device=arm_cpu -target=aarch64-linux-gnu')

# Also replace this with the device key in your tracker
device_key = 'rk3399'

# tuning option
network = 'resnet-18'
log_file = "%s.%s.log" % (device_key, network)
dtype = 'float32'

tuning_option = {
   'log_filename': log_file,

   'tuner':'xgb',
   'n_trial': 1000,
   'early_stopping': 200,

   'measure_option': autotvm.measure_option(
       autotvm.use_rpc(device_key, host='localhost', port=9190),
       number=4,
       parallel_num=1,
       timeout=10),

   'use_transfer_learning': True,
}

####################################################################
#
# .. note:: How to set tuning options
#
#   In general, the default value provided here works well. It is the same
#   value that we used to generate pre-tuned parameters.
#   If you have multiple devices, you can set :code:`parallel_num` to
#   the number of devices you have. (e.g. set it to 3 if you register 3 rk3399
#   boards to the tracker).
#   If you have large time budget, you can set :code:`n_trial`, :code:`early_stopping` larger,
#   which makes the tuning run longer.
#   If your device is very slow or a single conv2d operator in your network has large FLOPs,
#   consider setting timeout larger.
#
#   **For android phone**, add :code:`build_func='ndk'` to the argument list of
#   :code:`autotvm.measure_option` to use Android NDK for creating shared library.
#

###################################################################
# Begin Tuning
# ------------
# Now we can extract tuning tasks from the network and begin tuning.
# Here we provide a simple utility function to tune a list of tasks.
# This function is just an initial implementation which tune them in sequential order.
# Later we will bring more sophisticated tuner scheduler.

# You can skip the implementation of this function for this tutorial.
def tune_tasks(tasks,
               measure_option,
               tuner='xgb',
               n_trial=500,
               early_stopping=200,
               log_filename='tuning.log',
               use_transfer_learning=True,
               try_winograd=True):
    if try_winograd:
        for i in range(len(tasks)):
            try:  # try winograd template
                tsk = autotvm.task.create(tasks[i].name, tasks[i].args,
                                          tasks[i].target, tasks[i].target_host, 'winograd')
                tasks.append(tsk)
            except Exception:
                pass

    # create tmp log file
    tmp_log_file = log_filename + ".tmp"
    if os.path.exists(tmp_log_file):
        os.remove(tmp_log_file)

    for i, tsk in enumerate(tasks):
        prefix = "[Task %2d/%2d] " %(i+1, len(tasks))

        # create tuner
        if tuner == 'xgb' or tuner == 'xgb-rank':
            tuner_obj = XGBTuner(tsk, loss_type='rank')
        elif tuner == 'ga':
            tuner_obj = GATuner(tsk, pop_size=50)
        elif tuner == 'random':
            tuner_obj = RandomTuner(tsk)
        elif tuner == 'gridsearch':
            tuner_obj = GridSearchTuner(tsk)
        else:
            raise ValueError("Invalid tuner: " + tuner)

        if use_transfer_learning:
            if os.path.isfile(tmp_log_file):
                tuner_obj.load_history(autotvm.record.load_from_file(tmp_log_file))

        # do tuning
        tuner_obj.tune(n_trial=min(n_trial, len(tsk.config_space)),
                       early_stopping=early_stopping,
                       measure_option=measure_option,
                       callbacks=[
                           autotvm.callback.progress_bar(n_trial, prefix=prefix),
                           autotvm.callback.log_to_file(tmp_log_file)])

    # pick best records to a cache file
    autotvm.record.pick_best(tmp_log_file, log_filename)
    os.remove(tmp_log_file)


########################################################################
# Finally we launch tuning jobs and evaluate the end-to-end performance.

def tune_and_evaluate():
    # extract workloads from nnvm graph
    net, params, shape, out_shape = get_network(network, batch_size=1)
    tasks = autotvm.task.extract_from_graph(net, shape=shape, dtype=dtype,
                                            symbols=(nnvm.sym.conv2d,),
                                            target=target)

    # run tuning tasks
    tune_tasks(tasks, **tuning_option)

    # compile kernels with history best records
    with autotvm.apply_history_best(log_file):
        print("Compile...")
        with nnvm.compiler.build_config(opt_level=2, add_pass=['AlterOpLayout']):
            graph, lib, params = nnvm.compiler.build(
                net, target=target,
                shape=shape, params=params, dtype=dtype)

        # export library
        tmp = tempdir()
        if tuning_option['measure_option']['build_func'] == 'ndk': # for android
            from tvm.contrib import ndk
            filename = "net.so"
            lib.export_library(tmp.relpath(filename), ndk.create_shared)
        else:
            filename = "net.tar"
            lib.export_library(tmp.relpath(filename))

        # upload module to device
        print("Upload...")
        remote = autotvm.measure.request_remote(device_key, timeout=10000)
        remote.upload(tmp.relpath(filename))
        rlib = remote.load_module(filename)

        # upload parameters to device
        ctx = remote.context(str(target), 0)
        rparams = {k: tvm.nd.array(v, ctx) for k, v in params.items()}
        data_tvm = tvm.nd.array((np.random.uniform(size=shape['data'])).astype(dtype))
        module = runtime.create(graph, rlib, ctx)
        module.set_input('data', data_tvm)
        module.set_input(**rparams)

        # evaluate
        print("Evaluate inference time cost...")
        ftimer = module.module.time_evaluator("run", ctx, number=1, repeat=10)
        prof_res = np.array(ftimer().results) * 1000 # convert to millisecond
        print("Mean inference time (std dev): %.2f ms (%.2f ms)" %
                (np.mean(prof_res), np.std(prof_res)))

# We do not run the tuning in our webpage server since it takes too long.
# Uncomment the following line to run by yourself.
# tune_and_evaluate()

######################################################################
# Sample Output
# -------------
# The tuning needs to train xgboost models and use them for prediction.
# So a high performance CPU is recommended.
# It takes about 1.5 hour on a 32T AMD Ryzen CPU.
# One sample output is
#
# .. code-block:: bash
#
#    [Task  1/16]  Current/Best:   13.15/  20.49 GFLOPS | Progress: (297/1000) | 348.51 s Done.
#    [Task  2/16]  Current/Best:   16.66/  22.64 GFLOPS | Progress: (475/1000) | 415.42 s Done.
#    [Task  3/16]  Current/Best:   10.33/  14.19 GFLOPS | Progress: (306/1000) | 239.61 s Done.
#    [Task  4/16]  Current/Best:   13.29/  20.88 GFLOPS | Progress: (242/1000) | 227.48 s Done.
#    [Task  5/16]  Current/Best:   13.28/  15.61 GFLOPS | Progress: (237/1000) | 191.56 s Done.
#    [Task  6/16]  Current/Best:   20.16/  23.86 GFLOPS | Progress: (315/1000) | 304.31 s Done.
#    [Task  7/16]  Current/Best:    9.22/  22.00 GFLOPS | Progress: (458/1000) | 433.26 s Done.
#    [Task  8/16]  Current/Best:   14.12/  17.80 GFLOPS | Progress: (270/1000) | 240.73 s Done.
#    [Task  9/16]  Current/Best:   14.59/  24.02 GFLOPS | Progress: (209/1000) | 213.61 s Done.
#    [Task 10/16]  Current/Best:    9.86/  21.74 GFLOPS | Progress: (367/1000) | 359.93 s Done.
#    [Task 11/16]  Current/Best:    5.01/  18.86 GFLOPS | Progress: (202/1000) | 191.18 s Done.
#    [Task 12/16]  Current/Best:    8.61/  25.23 GFLOPS | Progress: (220/1000) | 220.74 s Done.
#    [Task 13/16]  Current/Best:   10.87/  25.79 GFLOPS | Progress: (465/1000) | 902.14 s Done.
#    [Task 14/16]  Current/Best:   15.33/  29.38 GFLOPS | Progress: (239/1000) | 481.33 s Done.
#    [Task 15/16]  Current/Best:   12.09/  38.60 GFLOPS | Progress: (476/1000) | 928.35 s Done.
#    [Task 16/16]  Current/Best:   16.77/  47.08 GFLOPS | Progress: (255/1000) | 439.91 s Done.
#    Compile...
#    Upload...
#    Evaluate inference time cost...
#    Mean inference time (std dev): 156.51 ms (0.89 ms)
#
