"""Fail-closed rules. Never log a Terraform plan or state."""

import hashlib
import io
import json
import zipfile

REGION = 'sa-saopaulo-1'
SHAPE = 'VM.Standard.A1.Flex'
NAME = 'vps-ampere-a1'
STACK_NAME = 'vps-ampere-a1-gratis'
NETWORK = {
    'oci_core_vcn', 'oci_core_subnet', 'oci_core_internet_gateway',
    'oci_core_route_table', 'oci_core_security_list',
    'oci_core_network_security_group', 'oci_core_network_security_group_security_rule',
    'oci_core_dhcp_options',
}


class Blocked(Exception):
    """Contains only a fixed, public-safe explanation."""


def require(condition, message):
    if not condition:
        raise Blocked(message)


def stack_digest(config_zip, variables, terraform_version):
    """Hash normalized ZIP content; archive timestamps do not affect approval."""
    h = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(config_zip)) as archive:
        names = sorted(archive.namelist())
        require(len(names) == len(set(names)), 'ZIP contém nomes duplicados.')
        require(sum(x.file_size for x in archive.infolist()) <= 20_000_000,
                'Configuração excede o tamanho permitido para revisão.')
        for name in names:
            if not name.endswith('/'):
                content = archive.read(name)
                h.update(json.dumps([name, hashlib.sha256(content).hexdigest()]).encode())
    h.update(json.dumps(variables, sort_keys=True, separators=(',', ':')).encode())
    h.update(str(terraform_version).encode())
    return h.hexdigest()


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def validate_plan(plan, compartment_id):
    """Only one new A1 plus free basic networking; never update/delete resources."""
    require(plan.get('errored') is not True, 'Plano Terraform contém erro.')
    configuration = plan.get('configuration')
    require(isinstance(configuration, dict), 'Plano sem configuração verificável.')
    providers = configuration.get('provider_config', {})
    require(bool(providers), 'Plano sem provider verificável.')
    for provider in providers.values():
        require(provider.get('full_name') == 'registry.terraform.io/oracle/oci',
                'Provider não permitido: somente oracle/oci.')
    for node in walk(configuration):
        if isinstance(node, dict):
            require(not node.get('provisioners'), 'Provisioners não são permitidos.')
    instances = []
    for change in plan.get('resource_changes', []):
        kind = change.get('type', '')
        require(change.get('provider_name') == 'registry.terraform.io/oracle/oci',
                'Recurso usa provider não permitido.')
        actions = change.get('change', {}).get('actions')
        if change.get('mode') == 'data':
            require(actions in (['read'], ['no-op']), 'Operação de dados inesperada.')
            continue
        require(actions in (['create'], ['no-op']),
                'Plano altera, substitui ou remove recurso existente.')
        # Even no-op managed resources outside the whitelist require review.
        require(kind == 'oci_core_instance' or kind in NETWORK,
                'Plano contém recurso fora da lista gratuita permitida.')
        after = change.get('change', {}).get('after') or {}
        if kind == 'oci_core_instance':
            instances.append(change)
            require(after.get('display_name') == NAME, 'Nome da instância divergente.')
            require(after.get('shape') == SHAPE, 'Shape divergente.')
            require(after.get('compartment_id') == compartment_id, 'Compartment divergente.')
            shape = after.get('shape_config') or []
            require(len(shape) == 1 and shape[0].get('ocpus') == 2
                    and shape[0].get('memory_in_gbs') == 12, 'CPU/RAM divergentes.')
            source = after.get('source_details') or []
            require(len(source) == 1 and source[0].get('source_type') == 'image',
                    'Origem da instância deve ser uma imagem.')
            source = source[0]
            size = source.get('boot_volume_size_in_gbs')
            require(isinstance(size, (int, float)) and 0 < size <= 50,
                    'Boot volume deve ter tamanho explícito de até 50 GB.')
            require(source.get('boot_volume_vpus_per_gb') in (None, 10),
                    'Boot volume deve usar desempenho Balanced.')
            require(bool(source.get('source_id')), 'Imagem ainda desconhecida no plano.')
            require(not after.get('capacity_reservation_id'), 'Reserva de capacidade proibida.')
            require(not after.get('dedicated_vm_host_id'), 'Host dedicado proibido.')
    require(len(instances) == 1, 'Plano deve conter exatamente a instância pretendida.')
    target = instances[0]
    require(target['change']['actions'] == ['create'],
            'Plano não cria a instância; reconciliar estado antes de continuar.')
    values = target['change']['after']
    require(bool(values.get('availability_domain')), 'Availability domain desconhecido.')
    return values


def state_instance_ids(state):
    """Get identity from Resource Manager state, never from a display name alone."""
    result = []
    for resource in state.get('resources', []):
        if resource.get('mode') == 'managed' and resource.get('type') == 'oci_core_instance':
            for instance in resource.get('instances', []):
                attrs = instance.get('attributes', {})
                require(attrs.get('display_name') == NAME and attrs.get('shape') == SHAPE,
                        'Estado da stack contém outra instância; revisão necessária.')
                require(bool(attrs.get('id')), 'Instância sem identidade no estado.')
                result.append(attrs['id'])
    require(len(result) <= 1, 'Estado da stack contém múltiplas instâncias.')
    return result


def capacity_only(messages):
    """Retry only known capacity failures, never mixed configuration failures."""
    text = '\n'.join(messages).lower()
    capacity = any(x in text for x in (
        'out of host capacity', 'out of capacity for shape', 'out of capacity for host',
    ))
    unsafe = any(x in text for x in (
        'notauthorized', 'not authorized', 'notauthenticated', 'not authenticated',
        'limitexceeded', 'limit exceeded', 'quotaexceeded', 'quota exceeded',
        'invalidparameter', 'invalid parameter', 'invalid configuration',
        'too many requests', 'toomanyrequests',
    ))
    errors = [x.lower() for x in messages if 'error:' in x.lower()]
    # Each Terraform error must itself identify capacity; unknown errors pause.
    return capacity and not unsafe and all(
        any(token in error for token in ('out of host capacity', 'out of capacity for shape',
                                        'out of capacity for host')) for error in errors)
