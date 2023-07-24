#####################################################################################
# Copyright (c) 2022-present, Advanced Micro Devices, Inc. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#####################################################################################

from typing import Sequence

import torch
from torch.fx.passes.shape_prop import ShapeProp
import migraphx

from torch_migraphx.fx.mgx_module import MGXModule
from torch_migraphx.fx.fx2mgx import MGXInterpreter
from torch_migraphx.fx.passes.pass_utils import validate_inference

from .passes.pass_manager import run_aten_passes
from .passes.partition import partition, get_partition_inputs
from .utils import print_graph_info


def lower_aten_to_mgx(gm: torch.fx.GraphModule,
                      example_inputs: Sequence[torch.Tensor],
                      **kwargs) -> torch.fx.GraphModule:
    """Lower aten GraphModule generated by dynamo and AOT Autograd to MIGraphX.
       1) Partition the graph into supported and unsupported subgraphs
       2) Lower to each supported subgraph to MIGraphX
       3) Replace original GraphModules with lowered MGXModules

    Args:
        gm (torch.fx.GraphModule): Graph generated by dynamo and aot autograd
        example_inputs (Sequence[torch.Tensor]): Example inputs

    Returns:
        torch.fx.GraphModule: GraphModule contatning MGXModule objects for supported subgraphs
    """
    verbose = kwargs['verbose'] if 'verbose' in kwargs else False
    if verbose:
        print_graph_info('Traced Model', gm, example_inputs)

    optim_gm = run_aten_passes(gm, example_inputs, verbose=verbose)
    del gm

    for name, mod in optim_gm.named_children():
        partition_inputs = get_partition_inputs(optim_gm, mod,
                                                example_inputs)
        if verbose:
            print_graph_info(name, mod, partition_inputs)

        mgx_mod = lower_subgraph(mod, partition_inputs, name=name, **kwargs)

        setattr(optim_gm, name, mgx_mod)
        del mod
        del partition_inputs

    return optim_gm


# @validate_inference(0.1, 0.1)
def lower_subgraph(module: torch.fx.GraphModule,
                   inputs: Sequence[torch.Tensor], **kwargs) -> MGXModule:
    """Lower graph to migraphx module. This graph should only contain supported nodes.

    Args:
        module (torch.fx.GraphModule): Graph to compile in MIGraphX
        inputs (Sequence[torch.Tensor]): Example inputs to the graph

    Returns:
        MGXModule: Callable module that executes graph via MIGraphX
    """

    ShapeProp(module).propagate(*inputs)

    verbose = kwargs['verbose'] if 'verbose' in kwargs else False
    fp16 = kwargs['fp16'] if 'fp16' in kwargs else False
    save_mxr = kwargs['save_mxr'] if 'save_mxr' in kwargs else False

    interpreter = MGXInterpreter(module, inputs, verbose_log=verbose)
    interpreter.run()

    if save_mxr:
        name = f"{kwargs['name']}.mxr" if 'name' in kwargs else "prog.mxr"
        migraphx.save(interpreter.program, name)

    mgx_module = MGXModule(program=interpreter.program,
                           input_names=interpreter.get_input_names(),
                           quantize_fp16=fp16)

    return mgx_module