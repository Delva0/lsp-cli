"""Source investigation workflows across versions, names and partial inputs."""

import json

import pytest

from .test_cli import SPAN
from .test_cypher import invoke, query, values
from .test_git_sources import git, write
from .test_git_sources import repository as repository


def test_cross_revision_sources_diff_and_original_hunk_coordinates(repository):
    body = query('MATCH (a:Symbol {root:true,revision:"v1"}),(b:Symbol {root:true,revision:"v2"}) '
                 'RETURN a.source_code,b.source_code,diff(a.source_code,b.source_code)',
                 'v1:Base.ping', 'v2:Base.ping', cwd=repository)
    assert len(values(body)) == 1
    old, new, diff = values(body)[0]
    assert old['source'] == 'v1:pkg/base.py' and new['source'] == 'v2:src/pkg/base.py'
    assert old['git_commit'] != new['git_commit']
    assert diff['status'] == 'modified'
    assert '@@ -2 +2 @@' in diff['patch']
    assert '- def ping(self): return 1' in diff['patch'] and '+ def ping(self): return 2' in diff['patch']
    assert diff['before']['code'] == old['code'] and diff['after']['code'] == new['code']
    assert SPAN <= old.keys() and SPAN <= new.keys() and SPAN <= diff.keys()
    assert {e['source'] for e in body['results'][0]['evidence']} == {old['source'], new['source']}
    assert [i['status'] for i in body['inputs']] == ['resolved', 'resolved']
    assert git(repository, 'status', '--porcelain').endswith('src/pkg/base.py')


def test_revision_range_and_diff_projection_are_native(repository):
    body = query('MATCH (a:Symbol {root:true,revision:"v1"}),(b:Symbol {root:true,revision:"v2"}) '
                 'WITH diff(a.source_code,b.source_code) AS d RETURN d.status,d.patch',
                 'v1..v2:Base.ping', cwd=repository)
    assert values(body)[0][0] == 'modified'
    assert values(body)[0][1].startswith('--- v1:pkg/base.py\n+++ v2:src/pkg/base.py')


def test_revision_graphs_do_not_create_cross_version_call_paths(repository):
    body = query('MATCH p=(a:Symbol {revision:"v1"})-[:CALLS*1..5]->(b:Symbol {revision:"v2"}) RETURN p',
                 'v1:pkg.client.start', 'v2:pkg.client.start', cwd=repository)
    assert not values(body)
    within = query('MATCH p=(s:Symbol {root:true})-[:CALLS* SHORTEST 1..8]->'
                   '(t:Symbol {name:"pkg.base.Base.ping"}) RETURN s.revision,p ORDER BY s.revision',
                   'v1:pkg.client.start', 'v2:pkg.client.start', cwd=repository)
    assert [v[0] for v in values(within)] == ['v1', 'v2']
    for revision, path in values(within):
        assert {n['revision'] for n in path['nodes']} == {revision}
        assert {e['revision'] for e in path['relationships']} == {revision}


@pytest.mark.parametrize('address', ['v1:pkg/worker.py', 'v2:src/pkg/worker.py'])
def test_file_addresses_select_original_module(repository, address):
    body = query('MATCH (s:Symbol {root:true}) RETURN s.source_code', address, cwd=repository)
    view = values(body)[0][0]
    assert view['symbol'] == 'pkg.worker' and view['source'] == address
    assert view['code'] == git(repository, 'show', address)


@pytest.mark.parametrize('revision,path', [('v1', 'pkg/worker.py'), ('v2', 'src/pkg/worker.py')])
def test_module_basename_without_self_reference_reads_same_blob(repository, revision, path):
    body = query('MATCH (s:Symbol {root:true}) RETURN s.source_code',
                 f'{revision}:worker', cwd=repository)
    view = values(body)[0][0]
    assert view['symbol'] == 'pkg.worker' and view['symbol_kind'] == 'module'
    assert view['code'] == git(repository, 'show', f'{revision}:{path}')
    assert view['git_blob'] == git(repository, 'rev-parse', f'{revision}:{path}')


def test_package_basename_reads_initializer(repository):
    write(repository, 'src/pkg/network/__init__.py', 'DEFAULT = 1\n')
    git(repository, 'add', 'src/pkg/network/__init__.py')
    git(repository, 'commit', '-qm', 'nested package')
    body = query('MATCH (s:Symbol {root:true}) RETURN s.source_code', 'HEAD:network', cwd=repository)
    view = values(body)[0][0]
    assert view['symbol'] == 'pkg.network' and view['code'] == 'DEFAULT = 1'


@pytest.mark.parametrize('second', ['module', 'definition'])
def test_short_module_name_collision_returns_candidates_without_guessing(repository, second):
    path, source = (('other/worker.py', 'VALUE = 2\n') if second == 'module' else
                    ('other/consumer.py', 'def worker(): return 2\n'))
    write(repository, path, source)
    git(repository, 'add', path)
    git(repository, 'commit', '-qm', 'ambiguous short name')
    result = invoke('HEAD:worker', 'MATCH (s:Symbol {root:true}) RETURN s.source_code',
                    '--json', cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'ambiguous'
    assert not values(body)
    expected = 'other.worker' if second == 'module' else 'other.consumer.worker'
    assert {c['name'] for c in body['inputs'][0]['candidates']} == {'pkg.worker', expected}


def test_wrong_module_address_offers_actual_module_and_explains_resolution_failure(repository):
    result = invoke('v1:wrong.worker', 'MATCH (s:Symbol {root:true}) RETURN s.source_code',
                    '--json', cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'not_found'
    assert 'could not be resolved' in body['inputs'][0]['message']
    assert any(c['address'] == 'v1:pkg.worker' for c in body['inputs'][0]['candidates'])
    assert not values(body)


def test_module_discovery_budget_does_not_claim_uniqueness(repository):
    write(repository, 'other/worker.py', 'VALUE = 2\n')
    git(repository, 'add', 'other/worker.py')
    git(repository, 'commit', '-qm', 'limited module discovery')
    result = invoke('HEAD:worker', 'MATCH (s:Symbol {root:true}) RETURN s.source_code',
                    '--max-files', '1', '--json', cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'incomplete'
    assert not values(body)


@pytest.mark.parametrize('selector', ['PoolKey', 'pkg.keys.PoolKey', 'src/pkg/keys.py'])
def test_assignment_names_read_without_guessing_graph_kind(repository, selector):
    write(repository, 'src/pkg/keys.py', 'from collections import namedtuple\nPoolKey = namedtuple("PoolKey", "cert")\n')
    git(repository, 'add', 'src/pkg/keys.py')
    git(repository, 'commit', '-qm', 'binding source')
    body = query('MATCH (s:Symbol {root:true}) RETURN s.source_code', f'HEAD:{selector}', cwd=repository)
    view = values(body)[0][0]
    assert 'PoolKey = namedtuple' in view['code']
    assert view['symbol_kind'] == ('module' if selector.endswith('.py') else 'binding')
    assert view['line'] == (1 if selector.endswith('.py') else 2)


def test_short_name_prefers_definition_over_import_sites(repository):
    write(repository, 'src/pkg/keys.py', 'PoolKey = 1\n')
    write(repository, 'src/pkg/consumer.py', 'from .keys import PoolKey\n')
    git(repository, 'add', 'src/pkg/keys.py', 'src/pkg/consumer.py')
    git(repository, 'commit', '-qm', 'import sites')
    body = query('MATCH (s:Symbol {root:true}) RETURN s.name', 'HEAD:PoolKey', cwd=repository)
    assert values(body) == [['pkg.keys.PoolKey']]
    assert 'pkg.consumer' not in {s['module'] for s in body['sources']}
    explicit = query('MATCH (s:Symbol {root:true}) RETURN s.kind,s.source_code',
                     'HEAD:pkg.consumer.PoolKey', cwd=repository)
    assert values(explicit)[0][0] == 'import'


def test_assignment_names_and_reassignments_are_discoverable_in_defines():
    body = query('MATCH (m:Symbol {root:true})-[:DEFINES]->(s:Symbol) '
                 'WHERE s.name ENDS WITH ".SETTING" RETURN s.kind,s.source_code ORDER BY s.line',
                 source='SETTING = 1\nif flag:\n SETTING = 2\n')
    assert [v[1]['line'] for v in values(body)] == [1, 3]
    assert all(v[0] == 'binding' for v in values(body))
    assert len({v[1]['id'] for v in values(body)}) == 2
    rendered = invoke('MATCH (m:Symbol {root:true})-[:DEFINES]->(s:Symbol) '
                      'WHERE s.name ENDS WITH ".SETTING" RETURN collect(s.source_code)',
                      source='SETTING = 1\nif flag:\n SETTING = 2\n').stdout
    assert '1 | SETTING = 1' in rendered and '3 |  SETTING = 2' in rendered


def test_declaration_names_respect_global_nonlocal_and_lambda_scopes():
    source = ('SETTING = 0\ndef change():\n global SETTING\n SETTING = 1\n'
              'def outer():\n value = 1\n def inner():\n  nonlocal value\n  value = 2\n'
              ' return inner\nfn = lambda: (inside := 3)\n')
    body = query('MATCH (s:Symbol) WHERE s.kind="binding" RETURN s.name,s.line ORDER BY s.line', source=source)
    declarations = values(body)
    assert ['__stdin__.SETTING', 4] in declarations
    assert ['__stdin__.outer.value', 9] in declarations
    assert ['__stdin__.change.SETTING', 4] not in declarations
    assert any('.<lambda>@' in name and name.endswith('.inside') for name, _ in declarations)


def test_limited_short_name_discovery_reports_uncertain_uniqueness(repository):
    write(repository, 'src/pkg/a.py', 'SETTING = 1\n')
    write(repository, 'src/pkg/b.py', 'SETTING = 2\n')
    git(repository, 'add', 'src/pkg/a.py', 'src/pkg/b.py')
    git(repository, 'commit', '-qm', 'limited discovery')
    result = invoke('HEAD:SETTING', 'MATCH (s:Symbol {root:true}) RETURN s.source_code',
                    '--max-files', '1', '--json', cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'incomplete'
    assert not values(body)


@pytest.mark.parametrize('bad', ['v1:NotThere', 'bad_revision:Worker.run', 'v1:src/pkg/worker.py'])
def test_partial_input_keeps_good_source_and_returns_nonzero(repository, bad):
    result = invoke('v1:Worker.run', bad,
                    'MATCH (s:Symbol {root:true}) RETURN s.source_code', '--json', cwd=repository)
    assert result.returncode == 2
    body = json.loads(result.stdout)
    assert body['status'] == 'partial'
    assert body['inputs'][0]['status'] == 'resolved' and body['inputs'][1]['status'] != 'resolved'
    assert values(body)[0][0]['symbol'] == 'pkg.worker.Worker.run'
    assert not body['coverage']['complete']
    assert 'Traceback' not in result.stdout + result.stderr


def test_wrong_module_returns_verified_candidate_address(repository):
    result = invoke('v1:pkg.client.Base', 'MATCH (s:Symbol {root:true}) RETURN s.source_code',
                    '--json', cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and not values(body)
    assert body['coverage']['scope'] == 'no_resolved_inputs'
    assert body['coverage']['versions'] == []
    candidates = body['inputs'][0]['candidates']
    assert any(c['address'] == 'v1:pkg.base.Base' and c['source'] == 'v1:pkg/base.py' for c in candidates)


def test_ambiguous_bindings_preserve_other_input_results(repository):
    write(repository, 'src/pkg/one.py', 'SETTING = 1\n')
    write(repository, 'src/pkg/two.py', 'SETTING = 2\n')
    git(repository, 'add', 'src/pkg/one.py', 'src/pkg/two.py')
    git(repository, 'commit', '-qm', 'ambiguous bindings')
    result = invoke('HEAD:SETTING', 'HEAD:Worker.run',
                    'MATCH (s:Symbol {root:true}) RETURN s.source_code', '--json', cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body['inputs'][0]['status'] == 'ambiguous'
    assert {c['name'] for c in body['inputs'][0]['candidates']} == {'pkg.one.SETTING', 'pkg.two.SETTING'}
    assert values(body)[0][0]['symbol'] == 'pkg.worker.Worker.run'


def test_missing_match_diff_does_not_assert_added_or_deleted(repository):
    body = query('MATCH (b:Symbol {root:true}) RETURN diff(NULL,b.source_code)', 'v1:Worker.run', cwd=repository)
    diff = values(body)[0][0]
    assert diff['status'] == 'unavailable' and diff['before'] is None and not diff['patch']
    assert len(body['results'][0]['evidence']) == 1


def test_diff_rejects_modified_source_evidence():
    result = invoke('MATCH (s:Symbol) WHERE s.name ENDS WITH ".leaf" '
                    'RETURN diff(struct_pack(kind:="SOURCE",symbol:=s.name,symbol_kind:=s.kind,'
                    'source:=s.source,line:=s.line,`column`:=s.`column`,end_line:=s.end_line,'
                    'end_column:=s.end_column,code:="invented",revision:="",git_commit:=NULL,'
                    'git_blob:=NULL,git_path:=NULL),s.source_code)', '--json')
    assert result.returncode == 2
    assert 'original source_code' in json.loads(result.stdout)['error']['message']


def test_nested_source_rendering_and_display_deduplication():
    text = 'MATCH (s:Symbol) WHERE s.name ENDS WITH ".leaf" RETURN {bodies:collect(s.source_code)},s.source_code'
    result = invoke(text)
    assert result.returncode == 0
    assert '1 | def leaf(): pass' in result.stdout
    assert result.stdout.count('1 | def leaf(): pass') == 1
    assert 'source already displayed above' in result.stdout
    assert '\\n' not in result.stdout
    body = query(text)
    assert values(body)[0][0]['bodies'][0]['code'] == values(body)[0][1]['code']


def test_bare_revision_has_module_roots_and_global_name_search(repository):
    body = query('MATCH (s:Symbol {root:true}) RETURN s.kind,s.name ORDER BY s.name', 'v1', cwd=repository)
    assert values(body) and all(v[0] == 'module' for v in values(body))
    assert ['module', 'pkg.worker'] in values(body)
    override = query('MATCH (s:Symbol {root:true}) RETURN s.kind,s.name ORDER BY s.name',
                     'v1', '--repo', str(repository))
    assert values(override) == values(body)


def test_multiple_native_statements_and_writes_still_rejected(repository):
    for text in ('RETURN 1; RETURN 2', 'MATCH (s:Symbol) SET s.name="bad" RETURN s'):
        result = invoke('v1:Base.ping', 'v2:Base.ping', text, '--json', cwd=repository)
        assert result.returncode == 2
        assert json.loads(result.stdout)['error']['kind'] == 'CypherError'
