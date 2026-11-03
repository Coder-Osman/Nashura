"""One isolated CUDA correctness/update/recovery probe on exported real FAIR inputs."""

import argparse
import json
from pathlib import Path
import time

import torch

from ..fair_search.contracts import semantic_digest
from .model import initialize_fresh_model
from .tower_bootstrap import SEED, soft_loss
from .tower_shared import load_data, create_optimizer, _save, _load, source_sha, file_sha
from .value_learning import policy_head_sha


def run(data_dir, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    data, _, manifest = load_data(data_dir)
    initial = torch.load(Path(data_dir)/"initial.pt", weights_only=True, map_location="cpu")
    train = data["training"]
    indices = torch.linspace(0, len(train["records"])-1, 16).long()
    frame = train["frame"].index_select(indices)
    hands, targets = train["hands"][indices], train["targets"][indices]
    cpu = initialize_fresh_model(SEED).eval()
    cpu.load_state_dict(initial["model"])
    with torch.inference_mode():
        started = time.perf_counter()
        cpu_logits = cpu.value_batch(frame,hands)
        cpu_seconds = time.perf_counter()-started
    model = initialize_fresh_model(SEED)
    model.load_state_dict(initial["model"])
    assert semantic_digest(model.state_dict()) == manifest["identity"]["initial_model_sha"]
    model.to("cuda:0").eval()
    optimizer = create_optimizer(model)
    policy_sha = policy_head_sha(model)
    buffers_sha = semantic_digest(dict(model.named_buffers()))
    with torch.inference_mode():
        model.value_batch(frame,hands)
        torch.cuda.synchronize()
        started = time.perf_counter()
        cuda_logits = model.value_batch(frame,hands)
        torch.cuda.synchronize()
        cuda_seconds = time.perf_counter()-started
    max_error = float((cuda_logits.cpu()-cpu_logits).abs().max())
    torch.testing.assert_close(cuda_logits.cpu(),cpu_logits,atol=2e-6,rtol=2e-4)

    def update():
        model.train()
        optimizer.zero_grad(set_to_none=True)
        loss = soft_loss(model.value_batch(frame,hands),targets.to("cuda:0"))
        loss.backward()
        gradients = {prefix:sum(float(p.grad.abs().sum()) for name,p in model.named_parameters()
            if name.startswith(prefix+".") and p.grad is not None)
            for prefix in ("encoder","hand_projection","trunk","value_head")}
        assert all(v > 0 for v in gradients.values())
        assert all(p.grad is None for p in model.policy_head.parameters())
        norm = torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad],1.)
        assert bool(torch.isfinite(norm))
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        return float(loss),gradients

    started = time.perf_counter()
    loss,gradients = update()
    torch.cuda.synchronize()
    update_seconds = time.perf_counter()-started
    identity = dict(protocol="sspi-tower-shared-cuda-probe.v1",source_sha=source_sha(),
        dataset_sha=file_sha(Path(data_dir)/"dataset.json"))
    _save(output_dir/"boundary.pt",model,optimizer,identity,dict(epoch=0,steps=1))
    random_expected = torch.rand(16,device="cuda:0")
    update()
    expected = semantic_digest((model.state_dict(),optimizer.state_dict()))
    _load(output_dir/"boundary.pt",model,optimizer,identity,"cuda:0")
    assert torch.equal(torch.rand(16,device="cuda:0"),random_expected)
    update()
    assert semantic_digest((model.state_dict(),optimizer.state_dict())) == expected
    assert policy_head_sha(model) == policy_sha
    assert semantic_digest(dict(model.named_buffers())) == buffers_sha
    report = dict(identity=identity,torch=str(torch.__version__),gpu=torch.cuda.get_device_name(),
        sample_count=len(indices),train_samples=len(train["records"]),validation_samples=len(data["validation"]["records"]),
        cpu_cuda_max_logit_error=max_error,cpu_forward_seconds=cpu_seconds,cuda_warm_forward_seconds=cuda_seconds,
        cuda_update_seconds=update_seconds,soft_ce=loss,gradient_l1=gradients,
        peak_cuda_bytes=torch.cuda.max_memory_allocated(),cuda_rng_restored=True,
        next_update_bitwise_equal=True,policy_head_unchanged=True,buffers_unchanged=True,
        timing_scope="single correctness probe, no backend performance conclusion")
    (output_dir/"probe.json").write_text(json.dumps(report,indent=2,allow_nan=False)+"\n")
    return report


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    print(json.dumps(run(**vars(parser.parse_args())),allow_nan=False))
