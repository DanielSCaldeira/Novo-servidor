"""One short reconciliation per execution; no sleeps and no local cloud state."""

import hashlib
import uuid
from datetime import datetime, timezone

from scripts.safety import Blocked, NAME, SHAPE, capacity_only, require, validate_plan

ACTIVE = {'ACCEPTED', 'IN_PROGRESS', 'CANCELING', 'CANCELLING'}
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELED', 'CANCELLED'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fresh():
    return {'version': 1, 'phase': 'ready', 'seq': 0}


def terminal_notice(gh, state):
    if state['phase'] == 'complete':
        text = (f'A VPS **vps-ampere-a1** foi confirmada em **RUNNING** em '
                f'{state["completed_at"]} (UTC). As tentativas terminaram. '
                'Consulte o IP e o acesso no console da Oracle.')
        key = 'complete'
    else:
        text = 'As tentativas foram pausadas: ' + state['reason']
        key = f'blocked-{state["seq"]}'
    gh.notify(key, text)
    gh.disable()


def block(gh, state, reason):
    state.update(phase='blocked', reason=reason)
    gh.save(state)
    terminal_notice(gh, state)


def submit(oracle, gh, state, operation, plan_id=None):
    # Persist token BEFORE the OCI call. A crash can safely recover the same request.
    state['seq'] += 1
    state.update(phase='pending', operation=operation, token=str(uuid.uuid4()),
                 pending_at=now(), plan_id=plan_id)
    state.pop('job_id', None)
    gh.save(state)
    recover_submission(oracle, gh, state, [])


def recover_submission(oracle, gh, state, jobs):
    token = state['token']
    label = 'free-vps-' + token
    matches = [job for job in jobs if job.display_name == label]
    require(len(matches) <= 1, 'Solicitação ambígua; revisar jobs antes de retomar.')
    if matches:
        job = matches[0]
    else:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(state['pending_at'])
        require(0 <= age.total_seconds() < 23 * 3600,
                'Solicitação pendente expirada; revisão manual evita duplicidade.')
        # A new/recovered Apply must still validate the approved plan and current usage.
        if state['operation'] == 'APPLY':
            target = validate_plan(oracle.plan(state['plan_id'], state['approved']), oracle.compartment_id)
            oracle.validate_capacity(target, oracle.inventory())
        job = oracle.create(state['operation'], label, token, state.get('plan_id'))
    require(job.operation == state['operation'], 'Operação do job recuperado divergente.')
    state.update(phase='tracking', job_id=job.id)
    gh.save(state)


def reconcile(oracle, gh, state, approved, resume=False):
    resuming = state['phase'] == 'blocked' and resume
    binding = hashlib.sha256(oracle.stack_id.encode()).hexdigest()
    require(state.get('binding', binding) == binding, 'Issue pertence a outra stack.')
    state['binding'] = binding
    if state['phase'] == 'complete':
        terminal_notice(gh, state)
        return
    if state['phase'] == 'blocked':
        if not resume:
            terminal_notice(gh, state)
            return
        # Resume re-evaluates the failed job and all safety checks below.
        state['phase'] = 'tracking' if state.get('job_id') else ('pending' if state.get('token') else 'ready')
        state.pop('reason', None)
        gh.save(state)
    require(len(approved) == 64 and oracle.digest() == approved,
            'Configuração da stack não corresponde ao SHA256 aprovado.')
    state['approved'] = approved
    jobs = oracle.jobs()
    require(all(j.lifecycle_state in ACTIVE | TERMINAL for j in jobs),
            'Job com estado desconhecido; revisão necessária.')
    # If the HTTP response was lost, first adopt a job by our durable request label.
    if state['phase'] == 'pending':
        matches = [j for j in jobs if j.display_name == 'free-vps-' + state['token']]
        if matches:
            recover_submission(oracle, gh, state, jobs)
        elif any(j.lifecycle_state in ACTIVE for j in jobs):
            return
        else:
            recover_submission(oracle, gh, state, jobs)
            return
    if any(j.lifecycle_state in ACTIVE for j in jobs):
        return
    instances = oracle.instances(jobs)
    inventory = oracle.inventory()
    known_ids = {instance.id for instance in instances}
    require(all(t.id in known_ids for t in inventory['targets']),
            'Instância com mesmo nome fora do estado da stack; nenhuma criação autorizada.')
    tracked = None
    if state.get('job_id'):
        tracked = next((j for j in jobs if j.id == state['job_id']), None)
        require(tracked is not None, 'Job acompanhado não encontrado.')
        require(tracked.operation == state['operation'], 'Tipo do job divergente.')
    if instances:
        instance = instances[0]
        latest_apply = next((j for j in jobs if j.operation == 'APPLY'), None)
        require(latest_apply is not None and latest_apply.lifecycle_state == 'SUCCEEDED',
                'Máquina existe após execução parcial; revisar stack sem recriar.')
        require(instance.lifecycle_state == 'RUNNING',
                'Instância já existe mas não está RUNNING; não recriar automaticamente.')
        require(instance.shape == SHAPE and instance.display_name == NAME,
                'Shape/nome da instância existente divergentes.')
        require(instance.shape_config.ocpus == 2 and instance.shape_config.memory_in_gbs == 12,
                'Instância existente tem CPU/RAM divergentes.')
        state.update(phase='complete', completed_at=now())
        gh.save(state)
        terminal_notice(gh, state)
        return
    if resuming and tracked and (tracked.operation == 'PLAN' or tracked.lifecycle_state != 'SUCCEEDED'):
        # An explicit resume acknowledges a terminal failure, but only a fresh,
        # validated Plan can authorize a subsequent Apply. Existing VMs block above.
        state['phase'] = 'ready'
        for key in ('job_id', 'operation', 'plan_id', 'token', 'pending_at'):
            state.pop(key, None)
        gh.save(state)
        tracked = None
    if tracked:
        status = tracked.lifecycle_state
        if status != 'SUCCEEDED':
            require(status == 'FAILED' and tracked.operation == 'APPLY'
                    and capacity_only(oracle.logs(tracked.id)),
                    'Job falhou sem confirmação exclusiva de falta de capacidade; revisar na Oracle.')
            # The next PLAN refreshes the same remote state and accounts for partial volumes.
            state.update(phase='ready')
            for key in ('job_id', 'operation', 'plan_id', 'token', 'pending_at'):
                state.pop(key, None)
            gh.save(state)
        elif tracked.operation == 'PLAN':
            target = validate_plan(oracle.plan(tracked.id, approved), oracle.compartment_id)
            oracle.validate_capacity(target, inventory)
            # Verify again immediately before submitting the mutation.
            require(oracle.digest() == approved, 'Stack mudou durante a validação.')
            submit(oracle, gh, state, 'APPLY', tracked.id)
            return
        else:
            raise Blocked('Apply terminou sem instância identificável no estado.')
    else:
        # Do not hide an existing unrelated failed stack job on first activation.
        if not resuming and jobs and jobs[0].lifecycle_state in {'FAILED', 'CANCELED', 'CANCELLED'}:
            require(jobs[0].operation == 'APPLY' and jobs[0].lifecycle_state == 'FAILED'
                    and capacity_only(oracle.logs(jobs[0].id)),
                    'Último job existente exige revisão antes de ativar tentativas.')
    require(inventory['storage_gb'] <= 150, 'Menos de 50 GB disponíveis; revisar volumes existentes.')
    require(inventory['a1_ocpus'] == 0 and inventory['a1_memory_gb'] == 0,
            'Franquia A1 já está em uso; nenhuma tentativa iniciada.')
    submit(oracle, gh, state, 'PLAN')
