"""OCI adapter: only Resource Manager can mutate cloud resources."""

import json
import os

import oci

from scripts.safety import (Blocked, NAME, REGION, SHAPE, STACK_NAME, require,
                            stack_digest, state_instance_ids)


def binary(response):
    data = response.data
    return b''.join(data.raw.stream(1024 * 1024, decode_content=True))


class Oracle:
    def __init__(self):
        self.stack_id = os.environ['OCI_STACK_ID']
        require(self.stack_id.startswith('ocid1.ormstack.oc1.sa-saopaulo-1.'), 'Stack fora de São Paulo.')
        if os.environ.get('OCI_PRIVATE_KEY'):
            require(os.environ['OCI_PRIVATE_KEY'].strip().startswith(
                ('-----BEGIN PRIVATE KEY-----', '-----BEGIN RSA PRIVATE KEY-----')),
                'OCI_PRIVATE_KEY deve conter o PEM completo da chave de API, não um caminho ou chave SSH.')
            config = {'tenancy': os.environ['OCI_TENANCY'].strip(), 'user': os.environ['OCI_USER'].strip(),
                      'fingerprint': os.environ['OCI_FINGERPRINT'].strip(),
                      'key_content': os.environ['OCI_PRIVATE_KEY'], 'region': REGION}
            if os.environ.get('OCI_KEY_PASSPHRASE'):
                config['pass_phrase'] = os.environ['OCI_KEY_PASSPHRASE']
        else:
            config = oci.config.from_file(profile_name=os.environ.get('OCI_PROFILE', 'DEFAULT'))
            require(config.get('region') == REGION, 'Perfil OCI deve usar São Paulo.')
        oci.config.validate_config(config)
        self.tenancy = config['tenancy']
        options = {'timeout': (10, 30), 'retry_strategy': oci.retry.NoneRetryStrategy()}
        self.rm = oci.resource_manager.ResourceManagerClient(config, **options)
        self.compute = oci.core.ComputeClient(config, **options)
        self.block = oci.core.BlockstorageClient(config, **options)
        self.identity = oci.identity.IdentityClient(config, **options)
        self.limits = oci.limits.LimitsClient(config, **options)
        self.stack = self.rm.get_stack(self.stack_id).data
        require(self.stack.display_name == STACK_NAME, 'Nome da stack divergente.')
        require(self.stack.lifecycle_state == 'ACTIVE', 'Stack não está ativa.')
        require(self.stack.config_source.config_source_type == 'ZIP_UPLOAD',
                'Use stack com configuração ZIP estática; fonte dinâmica requer revisão.')
        self.compartment_id = self.stack.compartment_id

    @staticmethod
    def all(method, *args, **kwargs):
        # SDK pagination helpers add their own long retry policy. Use explicit pages
        # so authentication/API failures return promptly without sleeping in a runner.
        result, seen = [], set()
        while True:
            response = method(*args, **kwargs)
            require(isinstance(response.data, list), 'Resposta paginada OCI inesperada.')
            result.extend(response.data)
            page = response.headers.get('opc-next-page')
            if not page:
                return result
            require(page not in seen, 'Paginação OCI repetida; interrompendo consulta.')
            seen.add(page)
            kwargs['page'] = page

    def digest(self):
        # Re-fetch variables too: changing only stack variables must revoke approval.
        self.stack = self.rm.get_stack(self.stack_id).data
        require(self.stack.config_source.config_source_type == 'ZIP_UPLOAD',
                'Fonte da configuração mudou; revisão necessária.')
        require(not self.stack.custom_terraform_provider and
                not self.stack.is_third_party_provider_experience_enabled,
                'Providers personalizados não são permitidos.')
        return stack_digest(binary(self.rm.get_stack_tf_config(self.stack_id)),
                            self.stack.variables, self.stack.terraform_version)

    def jobs(self):
        return self.all(self.rm.list_jobs, compartment_id=self.compartment_id,
                        stack_id=self.stack_id, sort_by='TIMECREATED', sort_order='DESC')

    def instances(self, jobs):
        # A missing state is only acceptable before an Apply has ever succeeded.
        try:
            state = json.loads(binary(self.rm.get_stack_tf_state(self.stack_id)))
        except oci.exceptions.ServiceError as error:
            if error.status != 404 or any(j.operation == 'APPLY' and j.lifecycle_state == 'SUCCEEDED'
                                          for j in jobs):
                raise
            state = {}
        ids = state_instance_ids(state)
        return [self.compute.get_instance(identifier).data for identifier in ids]

    def inventory(self):
        regions = self.all(self.identity.list_region_subscriptions, self.tenancy)
        require(any(r.region_name == REGION and r.is_home_region for r in regions),
                'São Paulo deve ser a home region para Always Free.')
        # ANY requires tenancy-wide inspect/read policies, documented in README.
        compartments = self.all(self.identity.list_compartments, self.tenancy,
                                compartment_id_in_subtree=True, access_level='ANY')
        compartments = [self.tenancy] + [c.id for c in compartments if c.lifecycle_state == 'ACTIVE']
        domains = self.all(self.identity.list_availability_domains, self.tenancy)
        total_gb = 0
        cores = memory = 0
        targets = []
        for compartment in compartments:
            for instance in self.all(self.compute.list_instances, compartment):
                if instance.lifecycle_state == 'TERMINATED':
                    continue
                if instance.display_name == NAME:
                    targets.append(instance)
                if instance.shape == SHAPE:
                    require(instance.shape_config is not None, 'Não foi possível medir A1 existente.')
                    cores += instance.shape_config.ocpus
                    memory += instance.shape_config.memory_in_gbs
            for volume in self.all(self.block.list_volumes, compartment_id=compartment):
                if volume.lifecycle_state != 'TERMINATED':
                    total_gb += volume.size_in_gbs
            for domain in domains:
                for volume in self.all(self.block.list_boot_volumes,
                                       availability_domain=domain.name, compartment_id=compartment):
                    if volume.lifecycle_state != 'TERMINATED':
                        total_gb += volume.size_in_gbs
        return {'storage_gb': total_gb, 'a1_ocpus': cores, 'a1_memory_gb': memory,
                'targets': targets}

    def validate_capacity(self, target, inventory):
        require(not inventory['targets'], 'Instância com nome pretendido já existe; reconciliar estado.')
        require(inventory['a1_ocpus'] + 2 <= 2 and inventory['a1_memory_gb'] + 12 <= 12,
                'A1 existente consome a franquia conservadora de 2 OCPUs/12 GB.')
        size = target['source_details'][0]['boot_volume_size_in_gbs']
        require(inventory['storage_gb'] + size <= 200, 'Disco excederia a franquia de 200 GB.')
        image = self.compute.get_image(target['source_details'][0]['source_id']).data
        require(image.operating_system == 'Oracle Linux' and
                image.operating_system_version.split('.')[0] == '9' and
                image.compartment_id is None and image.listing_type in (None, 'NONE') and
                image.launch_mode != 'EMULATED',
                'Imagem deve ser Oracle Linux 9 oficial (plataforma).')
        require(image.size_in_mbs is not None and image.size_in_mbs <= size * 1024,
                'Tamanho da imagem não cabe no disco aprovado.')
        # Check current regional/AD service limits, independently from Free Tier caps.
        limits = self.all(self.limits.list_limit_values, self.tenancy, service_name='compute')
        for name, needed in (('standard-a1-core-count', 2), ('standard-a1-memory-count', 12)):
            matching = [v for v in limits if v.name == name and
                        (v.scope_type == 'REGION' or v.availability_domain == target['availability_domain'])]
            require(bool(matching), 'Limite de serviço A1 não pôde ser confirmado.')
            for value in matching:
                kwargs = {}
                if value.scope_type == 'AD':
                    kwargs['availability_domain'] = target['availability_domain']
                for compartment in {self.tenancy, self.compartment_id}:
                    available = self.limits.get_resource_availability(
                        'compute', name, compartment, **kwargs).data.available
                    require(available is not None and available >= needed,
                            'Cota/limite A1 disponível insuficiente.')

    def plan(self, job_id, approved):
        job = self.rm.get_job(job_id).data
        require(job.stack_id == self.stack_id and job.operation == 'PLAN' and
                job.lifecycle_state == 'SUCCEEDED', 'Plano não pertence à stack ou não terminou.')
        snapshot = stack_digest(binary(self.rm.get_job_tf_config(job_id)), job.variables,
                                self.stack.terraform_version)
        require(snapshot == approved, 'Plano foi gerado com outra configuração da stack.')
        return json.loads(binary(self.rm.get_job_tf_plan(job_id, tf_plan_format='JSON')))

    def logs(self, job_id):
        # Fetch complete logs for classification, but never print or publish them.
        entries = self.all(self.rm.get_job_logs, job_id)
        if hasattr(entries, 'entries'):
            entries = entries.entries
        return [entry.message or '' for entry in entries]

    def create(self, operation, name, token, plan_id=None):
        if operation == 'PLAN':
            details = oci.resource_manager.models.CreatePlanJobOperationDetails(
                operation='PLAN', is_provider_upgrade_required=False)
        else:
            require(bool(plan_id), 'Apply exige plano validado.')
            details = oci.resource_manager.models.CreateApplyJobOperationDetails(
                operation='APPLY', execution_plan_strategy='FROM_PLAN_JOB_ID',
                execution_plan_job_id=plan_id, is_provider_upgrade_required=False)
        job = oci.resource_manager.models.CreateJobDetails(
            stack_id=self.stack_id, display_name=name, operation=operation,
            job_operation_details=details)
        return self.rm.create_job(job, opc_retry_token=token).data
