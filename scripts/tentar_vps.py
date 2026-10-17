"""Entrypoint. Diagnostic is read-only and is the default for local execution."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.controller import block, fresh, reconcile
from scripts.github_api import GitHub
from scripts.safety import Blocked, require


def enabled(name):
    return os.environ.get(name, '').lower() == 'true'


def diagnostic():
    from scripts.oracle_api import Oracle
    oracle = Oracle()
    jobs = oracle.jobs()
    inventory = oracle.inventory()
    oracle.instances(jobs)
    print('Diagnóstico somente leitura: nenhum job ou recurso foi criado.')
    print('APPROVED_STACK_SHA256=' + oracle.digest())
    print(f'Volumes na home region: {inventory["storage_gb"]} GB / 200 GB.')
    print(f'A1 existentes: {inventory["a1_ocpus"]} OCPUs / {inventory["a1_memory_gb"]} GB RAM.')
    print(f'Instâncias com nome pretendido: {len(inventory["targets"])}.')
    print('O hash identifica a configuração; não substitui sua revisão do Terraform.')


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
        print('Consulta concluída. Próxima verificação no próximo horário, sem espera no runner.')
        return 0
    except Exception as error:
        # OCI exception strings may contain request metadata. Never emit traceback/logs.
        reason = str(error) if isinstance(error, Blocked) else 'Falha de API/autenticação ou resposta inesperada; revisar configuração.'
        print('::error::' + reason)
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
