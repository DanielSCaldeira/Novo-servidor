"""Entrypoint. Diagnostic is read-only and is the default for local execution."""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.controller import block, fresh, reconcile
from scripts.github_api import GitHub
from scripts.safety import Blocked, capacity_only, require, validate_plan


def enabled(name):
    return os.environ.get(name, '').lower() == 'true'


def append_summary(markdown):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        try:
            with open(path, 'a', encoding='utf-8') as summary:
                summary.write(markdown + '\n\n')
        except OSError:
            print('::notice::Não foi possível gravar o resumo visual da execução.')


def public_error(error):
    """Expose error categories, never SDK messages/request data or credentials."""
    if isinstance(error, Blocked):
        return str(error)
    category = type(error).__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', category):
        category = 'UnexpectedError'
    status = getattr(error, 'status', None)
    code = getattr(error, 'code', '')
    operation = getattr(error, 'operation_name', '')
    details = [category]
    if isinstance(status, int):
        details.append(f'HTTP {status}')
    if isinstance(code, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', code):
        details.append(code)
    if isinstance(operation, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', operation):
        details.append(operation)
    return 'Falha OCI/API (' + ', '.join(details) + '); revisar credenciais, permissões ou configuração.'


def job_failure_summary(messages):
    """Publish a fixed classification, never Terraform logs or their values."""
    if capacity_only(messages):
        return 'Out of host capacity: falta temporária de capacidade para criar a instância.'
    text = '\n'.join(messages).lower()
    categories = (
        (('quotaexceeded', 'quota exceeded'), 'QuotaExceeded: cota do compartment excedida.'),
        (('limitexceeded', 'limit exceeded'), 'LimitExceeded: limite da tenancy excedido.'),
        (('notauthenticated', 'not authenticated'), 'NotAuthenticated: autenticação recusada.'),
        (('notauthorized', 'not authorized'), 'NotAuthorized: acesso recusado ou recurso não encontrado.'),
        (('toomanyrequests', 'too many requests'), 'TooManyRequests: limite de requisições excedido.'),
        (('invalidparameter', 'invalid parameter'), 'InvalidParameter: parâmetro inválido no provisionamento.'),
    )
    for needles, explanation in categories:
        if any(needle in text for needle in needles):
            return explanation
    return 'Erro não classificado automaticamente; detalhes completos permanecem no console privado OCI.'


def diagnostic():
    from scripts.oracle_api import Oracle
    oracle = Oracle()
    jobs = oracle.jobs()
    inventory = oracle.inventory()
    oracle.instances(jobs)
    print('Diagnóstico somente leitura: nenhum job ou recurso foi criado.')
    digest = oracle.digest()
    print('APPROVED_STACK_SHA256=' + digest)
    print(f'Volumes na home region: {inventory["storage_gb"]} GB / 200 GB.')
    print(f'A1 existentes: {inventory["a1_ocpus"]} OCPUs / {inventory["a1_memory_gb"]} GB RAM.')
    print(f'Instâncias com nome pretendido: {len(inventory["targets"])}.')
    print('O hash identifica a configuração; não substitui sua revisão do Terraform.')
    for job in jobs[:5]:
        print(f'Job recente: {job.operation} / {job.lifecycle_state}.')
    latest_apply = next((job for job in jobs if job.operation == 'APPLY'), None)
    failed = latest_apply if latest_apply and latest_apply.lifecycle_state == 'FAILED' else None
    result = 'Nenhum Apply encontrado na stack.'
    if failed:
        failure = job_failure_summary(oracle.logs(failed.id))
        print('Erro do último Apply falho: ' + failure)
        print('::warning title=Último Apply da Oracle falhou::' + failure)
        result = '> [!WARNING]\n> **Último Apply: FAILED**\n>\n> **' + failure + '**'
    elif latest_apply:
        result = f'**Último Apply: {latest_apply.lifecycle_state}.** Consulte o estado da instância para confirmar a criação.'
    append_summary(
        '## Oracle — diagnóstico\n\n' + result + '\n\n'
        '| Verificação | Resultado |\n| --- | --- |\n'
        f'| Armazenamento usado | {inventory["storage_gb"]} GB de 200 GB |\n'
        f'| A1 existentes | {inventory["a1_ocpus"]} OCPUs / {inventory["a1_memory_gb"]} GB RAM |\n'
        f'| Instâncias com o nome pretendido | {len(inventory["targets"])} |\n\n'
        'Esta execução é somente leitura e não cria recursos. **Diagnóstico bem-sucedido não confirma uma VPS criada.**')
    previous = next((job for job in jobs if job.operation == 'PLAN' and job.lifecycle_state == 'SUCCEEDED'), None)
    if previous:
        target = validate_plan(oracle.plan(previous.id, digest), oracle.compartment_id)
        oracle.validate_capacity(target, inventory)
        print('Plano existente validado: A1 2 OCPUs, 12 GB, Oracle Linux 9, disco até 50 GB; cotas disponíveis.')
    else:
        print('Ainda não há Plan concluído para validar antes do Apply.')


def main():
    gh = state = None
    try:
        if enabled('DIAGNOSTIC') or not os.environ.get('GITHUB_ACTIONS'):
            diagnostic()
            return 0
        gh = GitHub()
        gh.assert_public()
        state = gh.load() or fresh()
        if enabled('TEST_NOTIFICATION'):
            gh.save(state)
            test_key = 'notification-test-' + os.environ.get('GITHUB_RUN_ID', 'manual')
            gh.notify(test_key, 'Teste de aviso da VPS. Nenhuma chamada à Oracle foi feita. '
                      'Use Subscribe e confirme a entrega por e-mail antes de ativar.')
            print('Aviso de teste registrado na Issue de acompanhamento.')
            return 0
        # Complete/blocked notices must be recoverable even when retries are off.
        if state['phase'] in ('complete', 'blocked') and not enabled('RESUME_BLOCKED'):
            from scripts.controller import terminal_notice
            terminal_notice(gh, state)
            return 0
        require(enabled('RETRY_ENABLED'), 'RETRY_ENABLED não está habilitado.')
        require(enabled('ZERO_COST_CONFIRMED'), 'Validação de custo zero ainda não foi confirmada.')
        require(bool(os.environ.get('APPROVED_STACK_SHA256')), 'Hash aprovado ausente.')
        gh.save(state)
        from scripts.oracle_api import Oracle
        oracle = Oracle()
        reconcile(oracle, gh, state, os.environ['APPROVED_STACK_SHA256'], enabled('RESUME_BLOCKED'))
        append_summary('## Automação Oracle\n\n**Situação do controle: ' + state['phase'] + '**.\n\n'
                       'Um job registrado ainda precisa terminar na Oracle. Consulte a Issue de acompanhamento para a confirmação da VPS.')
        print('Consulta concluída. Próxima verificação no próximo horário, sem espera no runner.')
        return 0
    except Exception as error:
        # OCI exception strings may contain request metadata. Never emit traceback/logs.
        reason = public_error(error)
        print('::error::' + reason)
        append_summary('## Oracle — execução interrompida\n\n> [!CAUTION]\n> **' + reason + '**')
        if gh is not None and state is not None:
            try:
                # Preserve durable pending tokens after transport errors; recovering the
                # same request prevents duplicate jobs after manual correction/resume.
                if state['phase'] == 'complete':
                    gh.save(state)
                else:
                    block(gh, state, reason)
            except Exception:
                print('::error::Não foi possível concluir aviso/desativação; nenhuma nova criação nesta execução.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
