"""Receiver aliases must carry proof, preserve branch uncertainty and remain composable."""

import json

from .test_cypher import invoke, query, values
from .test_git_sources import git, write
from .test_git_sources import repository as repository


BODY = 'MATCH (s:Symbol {root:true}) RETURN s.name,s.receiver,s.source_code'


def local(text, *addresses, cypher=BODY):
    return invoke(*addresses, cypher, '--module', 'sample', '--json', source=text)


def test_alias_chain_resolves_method_with_source_proof():
    text = 'class Original:\n def run(self): pass\nFirst = Original\nAlias = First\n'
    result = local(text, 'Alias.run')
    body = json.loads(result.stdout)
    assert result.returncode == 0
    assert values(body)[0][:2] == ['sample.Original.run', 'sample.Original']
    binding = body['inputs'][0]['binding']
    assert binding['target'] == 'sample.Original.run'
    assert {e['text'] for e in binding['evidence'] if e['role'] == 'write'} == {'First = Original', 'Alias = First'}
    assert binding['evidence_complete']


def test_imported_alias_discovery_loads_real_definition_and_import_chain(repository):
    write(repository, 'src/pkg/aliases.py', 'from .worker import Worker as Imported\nPublic = Imported\n')
    git(repository, 'add', 'src/pkg/aliases.py')
    git(repository, 'commit', '-qm', 'reexport receiver alias')
    body = query(BODY, 'HEAD:Public.run', cwd=repository)
    assert values(body)[0][0] == 'pkg.worker.Worker.run'
    history = body['inputs'][0]['binding']['evidence']
    assert any(e['role'] == 'import' and 'Worker as Imported' in e['text'] for e in history)
    assert any(e['source'] == 'HEAD:src/pkg/worker.py' for e in history)


def test_conditional_alias_is_unresolved_with_precise_alternatives():
    text = ('class A:\n def run(self): pass\nclass B: pass\nAlias = A\n'
            'if flag:\n Alias = B\nFinal = Alias\n')
    result = local(text, 'Final.run', 'A.run')
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['status'] == 'partial'
    failed = body['inputs'][0]
    assert failed['status'] == 'unresolved'
    assert [s['name'] for s in failed['candidates']] == ['sample.A.run']
    binding = failed['binding']
    assert binding['target'] is None and binding['resolution'] == 'unresolved'
    assert {c['binding'] for c in binding['alternatives']} == {'sample.A', 'sample.B'}
    assert any(c['binding'] == 'sample.B' and c['resolution'] == 'unresolved' for c in binding['alternatives'])
    condition = next(e for e in binding['evidence'] if e['text'] == 'Alias = B')['context'][0]
    assert condition['kind'] == 'if' and condition['label'] == 'flag' and condition['line'] == 5
    assert values(body)[0][0] == 'sample.A.run'


def test_alias_snapshot_does_not_use_later_rebinding_as_its_evidence():
    text = ('class A:\n def run(self): pass\nclass B:\n def run(self): pass\n'
            'Alias = A\nSnapshot = Alias\nAlias = B\n')
    body = json.loads(local(text, 'Snapshot.run').stdout)
    assert values(body)[0][0] == 'sample.A.run'
    assert all(e['line'] < 7 for e in body['inputs'][0]['binding']['evidence'])
    current = json.loads(local(text, 'Alias.run').stdout)
    assert values(current)[0][0] == 'sample.B.run'


def test_same_binding_in_both_branches_remains_resolved():
    text = 'class A:\n def run(self): pass\nif flag:\n Alias = A\nelse:\n Alias = A\n'
    body = json.loads(local(text, 'Alias.run').stdout)
    assert values(body)[0][0] == 'sample.A.run'
    history = body['inputs'][0]['binding']['evidence']
    assert {c['kind'] for e in history for c in e['context']} == {'if', 'else'}


def test_alias_inherited_method_keeps_actual_receiver_and_mro():
    text = ('class Base:\n def hook(self): pass\n def run(self): self.hook()\n'
            'class Child(Base):\n def hook(self): pass\nAlias = Child\n')
    body = json.loads(local(text, 'Alias.run', cypher=
                       'MATCH (s:Symbol {root:true})-[c:CALLS]->(t) RETURN s.name,s.receiver,t.name').stdout)
    assert values(body) == [['sample.Base.run', 'sample.Child', 'sample.Child.hook']]
    assert body['inputs'][0]['binding']['mro'] == ['sample.Child', 'sample.Base', 'builtins.object']


def test_global_mutation_and_decorator_are_not_guessed():
    for text in (
        'class A:\n def run(self): pass\nAlias=A\ndef change():\n global Alias\n Alias=missing\n',
        '@unknown_decorator\nclass A:\n def run(self): pass\nAlias=A\n',
    ):
        result = local(text, 'Alias.run')
        body = json.loads(result.stdout)
        assert result.returncode == 2 and body['inputs'][0]['status'] == 'unresolved'
        assert not values(body)


def test_ambiguous_receiver_alias_does_not_select_an_arbitrary_same_named_method():
    text = ('class A:\n def run(self): pass\nclass One:\n Alias=A\n'
            'class Two:\n Alias=A\n')
    result = local(text, 'Alias.run')
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'ambiguous'
    assert {s['name'] for s in body['inputs'][0]['candidates']} == {'sample.One.Alias', 'sample.Two.Alias'}


def test_external_import_member_remains_unresolved_and_explains_import():
    result = local('from unavailable import Client as Alias\n', 'Alias.run')
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'unresolved'
    assert body['inputs'][0]['binding']['evidence'][0]['text'] == 'from unavailable import Client as Alias'
    assert not values(body)


def test_same_line_import_rebindings_retain_their_actual_statements():
    result = local('import unavailable_a as Alias; import unavailable_b as Alias\n', 'Alias.run')
    body = json.loads(result.stdout)
    assert result.returncode == 2
    history = body['inputs'][0]['binding']['evidence']
    assert [r['text'] for r in history] == ['import unavailable_a as Alias', 'import unavailable_b as Alias']


def test_readable_binding_feedback_shows_conditions_and_complete_body_markers():
    text = 'class A:\n def run(self): pass\nAlias=A\n'
    result = invoke('Alias.run', BODY, '--module', 'sample', source=text)
    assert result.returncode == 0
    assert 'binding: sample.Alias -> sample.A.run' in result.stdout
    assert 'write Alias=A' in result.stdout
    assert 'complete body, 1 lines' in result.stdout and 'END SOURCE sample.A.run' in result.stdout


def test_source_result_is_complete_while_call_graph_has_an_unknown_frontier():
    text = 'def run(client):\n client.unknown()\n'
    body = json.loads(local(text, 'run').stdout)
    assert body['status'] == 'incomplete' and not body['coverage']['complete']
    assert body['output']['status'] == 'complete'
    source = body['output']['source_bodies'][0]
    assert source['symbol'] == 'sample.run' and source['complete'] and source['lines'] == 2
    rendered = invoke('run', BODY, '--module', 'sample', source=text).stdout
    assert 'returned source: 1 complete bodies' in rendered and 'graph: incomplete' in rendered
    assert 'query: ok | results: complete' in rendered
    assert rendered.index('END SOURCE') < rendered.index('END RESULTS') < rendered.index('graph: incomplete')


def test_native_row_limit_does_not_claim_complete_results():
    body = query('MATCH (s:Symbol) RETURN s.source_code', '--max-rows', '1')
    assert body['output']['status'] == 'truncated'
    assert body['output']['source_bodies'][0]['complete']


def test_query_error_keeps_input_identity_and_error_in_the_text_result():
    result = invoke('MATCH (s:Symbol) RETURN s.missing_property')
    assert result.returncode == 2
    assert 'query: error' in result.stdout and 'query error (CypherError)' in result.stdout
    assert 'input[0]' in result.stdout and 'missing_property' in result.stdout
    assert not result.stderr


def test_unresolved_mro_text_identifies_affected_class_and_source():
    result = invoke('MATCH (s:Symbol {kind:"class"}) RETURN s.name',
                    '--module', 'sample', source='class Child(External): pass\n')
    assert result.returncode == 0
    assert 'graph unresolved_mro:' in result.stdout
    assert 'sample.Child @ <stdin>:1:0-1:27' in result.stdout


def test_diff_metadata_does_not_promise_two_printed_source_bodies(repository):
    diff = 'MATCH (a:Symbol {root:true,revision:"v1"}),(b:Symbol {root:true,revision:"v2"}) RETURN diff(a.source_code,b.source_code)'
    body = query(diff, 'v1..v2:Base.ping', cwd=repository)
    assert body['output']['source_bodies'] == [] and len(body['output']['diffs']) == 1
    together = query(diff + ',a.source_code,b.source_code', 'v1..v2:Base.ping', cwd=repository)
    assert len(together['output']['source_bodies']) == 2
