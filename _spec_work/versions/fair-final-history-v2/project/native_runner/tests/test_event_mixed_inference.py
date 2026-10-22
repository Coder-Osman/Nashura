from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_inference_service import requests
from native_runner.training import event_inference_service as inference


def test_two_models_route_and_batch_independently_with_round_barrier(monkeypatch):
    models = [_event_model().eval(), _event_model().eval()]
    with torch.no_grad():
        for p in models[1].parameters():
            p.add_(.007)
    descriptors = [dict(artifact_sha256=f'a{i}', source_checkpoint_sha256=f's{i}', contract_sha256='c') for i in range(2)]
    class Weights:
        def load(self, descriptor):
            if descriptor not in descriptors:
                raise ValueError('inference contract mismatch')
            i=descriptors.index(descriptor)
            self.model=models[i]
            return self.model, f'policy{i}'
    monkeypatch.setattr(inference, 'InferenceModel', Weights)
    observed=[]
    real_infer=inference.infer_batch
    def infer(model, reqs):
        assert len({tuple(r['identity']) for r in reqs})==1
        assert len({tuple(r['temperatures']) for r in reqs})==1
        observed.append((model,len(reqs)))
        return real_infer(model,reqs)
    monkeypatch.setattr(inference,'infer_batch',infer)
    service=inference.Service('cpu', max_batch=8, wait_ms=20)
    clients=[]
    with tempfile.TemporaryDirectory(prefix='cr-mix-',dir='/tmp') as directory:
        path=Path(directory)/'s'
        thread=threading.Thread(target=service.run,args=(path,),daemon=True);thread.start()
        for _ in range(200):
            if path.exists():break
            time.sleep(.005)
        control=inference.Client(path)
        try:
            control.request('load_set',descriptors=descriptors)
            control.request('begin_round',round_id='round1')
            for operation, payload in [('load',dict(descriptor=descriptors[0])),('load_set',dict(descriptors=descriptors))]:
                with pytest.raises(RuntimeError,match='frozen'):
                    control.request(operation,**payload)
            with pytest.raises(RuntimeError,match='identity'):
                control.request('end_round',round_id='wrong')
            with pytest.raises(RuntimeError,match='stale'):
                control.request('describe',identity=('bad','bad','bad'))
            reqs=requests(8)
            for i in range(8):
                client=inference.Client(path);clients.append(client)
                _,version=client.start_match(descriptors[i//4],100+i)
                assert version==f'policy{i//4}'
            barrier=threading.Barrier(8)
            def run(i):
                barrier.wait(timeout=10)
                r=reqs[i]
                return clients[i].infer(r['frame'],owner=i%2,tick=100,**{k:v for k,v in r.items() if k not in ('frame','uniforms')})
            with ThreadPoolExecutor(8) as pool:
                results=list(pool.map(run,range(8)))
            for i,actual in enumerate(results):
                expected=real_infer(models[i//4],[reqs[i]])[0]
                torch.testing.assert_close(actual.log_prob,expected.log_prob,atol=2e-5,rtol=2e-5)
                for name in ('act','card_row','cell','delay_bin'):
                    assert torch.equal(getattr(actual.action,name),getattr(expected.action,name))
            assert {id(m) for m,_ in observed}=={id(m) for m in models}
            control.request('end_round',round_id='round1')
            control.request('load_set',descriptors=[descriptors[1]])
            with pytest.raises(RuntimeError,match='stale'):
                clients[0].start_match(descriptors[0],1)
            with pytest.raises(RuntimeError,match='contract mismatch'):
                control.request('load_set',descriptors=[descriptors[0],dict(descriptors[1],contract_sha256='wrong')])
            _, version = clients[4].start_match(descriptors[1], 500)
            assert version == 'policy1'
        finally:
            for client in clients:client.close()
            control.request('shutdown');control.close();thread.join(timeout=10)
        assert not thread.is_alive()


def test_incompatible_timing_does_not_replace_published_model_set(monkeypatch):
    models = [_event_model().eval(), _event_model().eval()]
    models[1].event_config = replace(models[1].event_config, max_wait_ticks=301)
    descriptors = [dict(artifact_sha256=f'a{i}', source_checkpoint_sha256=f's{i}',
                        contract_sha256=f'c{i}') for i in range(2)]

    class Weights:
        def load(self, descriptor):
            i = descriptors.index(descriptor)
            self.model = models[i]
            return self.model, f'policy{i}'

    monkeypatch.setattr(inference, 'InferenceModel', Weights)
    service = inference.Service('cpu')
    published = service.control(dict(operation='load_set', descriptors=[descriptors[0]]))
    with pytest.raises(ValueError, match='incompatible'):
        service.control(dict(operation='load_set', descriptors=descriptors))
    result = service.control(dict(operation='describe', identity=published['identities'][0]))
    assert result['policy_version'] == 'policy0'
    assert result['model'].event_config == models[0].event_config
    assert list(service.models) == published['identities']
