import os

import torch


def configure():
    workspace = ":4096:8"
    if torch.cuda.is_initialized() and os.environ.get("CUBLAS_WORKSPACE_CONFIG") != workspace:
        raise RuntimeError("deterministic runtime must be configured before CUDA initialization")
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = workspace
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    return dict(event="deterministic_runtime_configured", torch=torch.__version__,
        cuda=torch.version.cuda, deterministic=torch.are_deterministic_algorithms_enabled(),
        warn_only=torch.is_deterministic_algorithms_warn_only_enabled(),
        cudnn_deterministic=torch.backends.cudnn.deterministic,
        cudnn_benchmark=torch.backends.cudnn.benchmark,
        matmul_allow_tf32=torch.backends.cuda.matmul.allow_tf32,
        cudnn_allow_tf32=torch.backends.cudnn.allow_tf32,
        cublas_workspace_config=os.environ["CUBLAS_WORKSPACE_CONFIG"])
