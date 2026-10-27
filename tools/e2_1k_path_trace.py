"""Bound natural PN backend ART windows without attributing unrelated thread activity."""

from e2_1_partial_method_trace import parse_partial
from verify_e2_gate import require


def parse_path(raw, native):
    owner = 'org.nashura.evidence.E21PreferenceObserver' if native else 'com.supercell.titan.ApplicationUtilBase'
    methods = {'nativeCommand'} if native else {'getKeyValue', 'storeKeyValue'}
    result = parse_partial(raw, owner, methods, allow_identical_metadata=True)
    roots = result['roots']
    require(roots and all(row['completion'] == 'returned' for row in roots), 'natural path root incomplete')
    if native:
        require(len(roots) == 6 and len({row['thread'] for row in roots}) == 1,
                'natural PN raw backend root count/thread differs')
    else:
        require(any(row['method'] == 'storeKeyValue' for row in roots),
                'original write root absent from observation window')
    inside = [call['method'] for row in roots for call in row['selected_calls']]
    cipher = sum(label.startswith('javax.crypto.Cipher.doFinal') for label in inside)
    old_store = sum(label.startswith(('c.a.j(', 'c.a.n(')) for label in inside)
    return dict(schema='e2.1k-natural-path-summary.v1', trace_sha256=result['trace_sha256'],
                records=result['records'], root_class=owner, root_methods=sorted(methods),
                identical_metadata_duplicates=result['identical_metadata_duplicates'],
                roots=roots, named_cipher_calls_inside_roots=cipher,
                named_old_store_calls_inside_roots=old_store,
                global_named_counts=result['selected_counts'], unnamed_methods=result['unnamed_methods'],
                exhaustive_method_coverage=False,
                coverage='complete named root windows; original roots include other configuration keys; arguments unavailable; unnamed methods opaque; other threads excluded',
                unnamed_method_semantics='unknown', no_vm_verified=False)
