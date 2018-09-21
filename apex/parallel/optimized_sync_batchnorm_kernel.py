import torch
from torch.autograd.function import Function

import syncbn

class SyncBatchnormFunction(Function):

    @staticmethod
    def forward(ctx, input, weight, bias, running_mean, running_variance, eps, track_running_stats = False, momentum = 1.0):
        torch.cuda.nvtx.range_push("sync_BN_fw")
        mean, var, var_biased = syncbn.welford_mean_var(input)

        if torch.distributed.is_initialized():
          world_size = torch.distributed.get_world_size()
          mean_all = torch.empty(mean.size(0), world_size, dtype=mean.dtype, device=mean.device)
          var_all = torch.empty(var.size(0), world_size, dtype=var.dtype, device=var.device)
          mean_l = [mean_all.narrow(1, i, 1) for i in range(mean_all.size(1))]
          var_l = [var_all.narrow(1, i, 1) for i in range(var_all.size(1))]
          torch.distributed.all_gather(mean_l, mean)
          torch.distributed.all_gather(var_l, var_biased)
          mean, var, var_biased = syncbn.welford_parallel(mean_all, var_all, int(input.numel()/input.size(1)))

        if track_running_stats:
          running_mean.data = running_mean.data * (1-momentum) + momentum*mean
          running_variance.data = running_variance.data * (1-momentum) + momentum*var

        ctx.save_for_backward(input, weight, bias, mean, var_biased)
        ctx.eps = eps

        out = syncbn.batchnorm_forward(input, mean, var_biased, weight, bias, eps)

        torch.cuda.nvtx.range_pop()
        return out

    @staticmethod
    def backward(ctx, grad_output):
        torch.cuda.nvtx.range_push("sync_BN_bw")
        # mini batch mean & var are calculated by forward path.
        # mu = 1./N*np.sum(h, axis = 0)
        # var = 1./N*np.sum((h-mu)**2, axis = 0)
        saved_input, weight, bias, running_mean, running_variance = ctx.saved_tensors
        eps = ctx.eps
        grad_input = grad_weight = grad_bias = None

        # TODO(jie): why do I have to clone here? life time of grad_output?
        mean_dy, mean_dy_xmu, grad_weight, grad_bias = syncbn.reduce_bn(grad_output, saved_input, running_mean, running_variance, eps)

        # calculate grad_input
        if ctx.needs_input_grad[0]:

            if torch.distributed.is_initialized():
                torch.distributed.all_reduce(
                    mean_dy, op=torch.distributed.reduce_op.SUM)
                mean_dy = mean_dy / torch.distributed.get_world_size()
                torch.distributed.all_reduce(
                    mean_dy_xmu, op=torch.distributed.reduce_op.SUM)
                mean_dy_xmu = mean_dy_xmu / torch.distributed.get_world_size()
            # tricky precision!!
            grad_input = syncbn.batchnorm_backward(grad_output, saved_input, running_mean, running_variance, weight, bias, mean_dy, mean_dy_xmu, eps)

        if weight is None or not ctx.needs_input_grad[1]:
            grad_weight = None

        if bias is None or not ctx.needs_input_grad[2]:
            grad_bias = None

        torch.cuda.nvtx.range_pop()
        return grad_input, grad_weight, grad_bias, None, None, None, None, None
