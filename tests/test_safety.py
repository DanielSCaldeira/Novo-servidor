import copy
import io
import unittest
import zipfile

from scripts.safety import Blocked, capacity_only, stack_digest, state_instance_ids, validate_plan

PROVIDER = 'registry.terraform.io/oracle/oci'


def plan():
    return {
        'configuration': {'provider_config': {'oci': {'full_name': PROVIDER}}, 'root_module': {
            'resources': [{'address': 'oci_core_instance.target', 'expressions': {}}]}},
        'resource_changes': [{
            'address': 'oci_core_instance.target', 'type': 'oci_core_instance',
            'mode': 'managed', 'provider_name': PROVIDER,
            'change': {'actions': ['create'], 'after': {
                'display_name': 'vps-ampere-a1', 'shape': 'VM.Standard.A1.Flex',
                'compartment_id': 'compartment', 'availability_domain': 'AD-1',
                'shape_config': [{'ocpus': 2, 'memory_in_gbs': 12}],
                'source_details': [{'source_type': 'image', 'source_id': 'image',
                                    'boot_volume_size_in_gbs': 50, 'boot_volume_vpus_per_gb': 10}],
            }},
        }],
    }


def archive(timestamp=(2025, 1, 1, 0, 0, 0), content=b'resource {}'):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr(zipfile.ZipInfo('main.tf', date_time=timestamp), content)
    return data.getvalue()


class SafetyTests(unittest.TestCase):
    def test_expected_plan_allowed(self):
        self.assertEqual(validate_plan(plan(), 'compartment')['shape'], 'VM.Standard.A1.Flex')

    def test_mutation_or_paid_resources_blocked(self):
        mutations = [
            lambda p: p['resource_changes'][0]['change'].update(actions=['update']),
            lambda p: p['resource_changes'][0]['change'].update(actions=['delete', 'create']),
            lambda p: p['resource_changes'][0]['change']['after'].update(shape='VM.Standard.A2.Flex'),
            lambda p: p['resource_changes'][0]['change']['after']['shape_config'][0].update(ocpus=4),
            lambda p: p['resource_changes'][0]['change']['after']['shape_config'][0].update(memory_in_gbs=24),
            lambda p: p['resource_changes'][0]['change']['after']['source_details'][0].update(boot_volume_size_in_gbs=51),
            lambda p: p['resource_changes'][0]['change']['after']['source_details'][0].update(boot_volume_size_in_gbs=None),
            lambda p: p['resource_changes'][0]['change']['after']['source_details'][0].update(boot_volume_vpus_per_gb=20),
            lambda p: p['resource_changes'][0]['change']['after'].update(compartment_id='other'),
            lambda p: p['resource_changes'][0]['change']['after'].update(capacity_reservation_id='reservation'),
            lambda p: p['resource_changes'][0].update(type='oci_core_nat_gateway'),
            lambda p: p['configuration']['root_module'].update(provisioners=[{'type': 'local-exec'}]),
            lambda p: p['configuration']['provider_config']['oci'].update(full_name='registry.terraform.io/hashicorp/external'),
            lambda p: p['resource_changes'].append(copy.deepcopy(p['resource_changes'][0])),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = plan()
                mutate(value)
                with self.assertRaises(Blocked):
                    validate_plan(value, 'compartment')

    def test_existing_amd_untouched_even_if_noop(self):
        value = plan()
        amd = copy.deepcopy(value['resource_changes'][0])
        amd['change']['actions'] = ['no-op']
        amd['change']['after']['shape'] = 'VM.Standard.E2.1.Micro'
        value['resource_changes'].append(amd)
        with self.assertRaises(Blocked):
            validate_plan(value, 'compartment')

    def test_implicit_paid_options_and_existing_disk_attachments_blocked(self):
        for field, setting in (
            ('launch_volume_attachments', [{'volume_id': 'existing-amd-disk'}]),
            ('instance_configuration_id', 'configuration-with-extra-volumes'),
            ('is_ai_enterprise_enabled', True),
            ('licensing_configs', [{'type': 'PAID'}]),
            ('preemptible_instance_config', [{'preemption_action': []}]),
        ):
            value = plan()
            value['resource_changes'][0]['change']['after'][field] = setting
            with self.assertRaises(Blocked):
                validate_plan(value, 'compartment')
        value = plan()
        value['resource_changes'][0]['change']['after_unknown'] = {'launch_volume_attachments': True}
        value['configuration']['root_module']['resources'][0]['expressions'] = {
            'launch_volume_attachments': {'references': ['var.extra_disks']}}
        with self.assertRaises(Blocked):
            validate_plan(value, 'compartment')

    def test_provider_computed_defaults_are_not_mistaken_for_paid_configuration(self):
        value = plan()
        value['resource_changes'][0]['change']['after_unknown'] = {
            'dedicated_vm_host_id': True, 'instance_configuration_id': True,
            'is_ai_enterprise_enabled': True, 'source_details': [{'kms_key_id': True}]}
        validate_plan(value, 'compartment')

    def test_paid_option_through_unknown_variable_is_blocked(self):
        value = plan()
        value['resource_changes'][0]['change']['after_unknown'] = {'is_ai_enterprise_enabled': True}
        value['configuration']['root_module']['resources'][0]['expressions'] = {
            'is_ai_enterprise_enabled': {'references': ['var.enable_paid_ai']}}
        with self.assertRaises(Blocked):
            validate_plan(value, 'compartment')

    def test_storage_and_other_paid_resources_refused(self):
        for kind in ('oci_core_volume', 'oci_core_boot_volume', 'oci_load_balancer_load_balancer',
                     'oci_core_public_ip', 'oci_core_service_gateway'):
            value = plan()
            extra = copy.deepcopy(value['resource_changes'][0])
            extra['type'] = kind
            value['resource_changes'].append(extra)
            with self.assertRaises(Blocked):
                validate_plan(value, 'compartment')

    def test_free_networking_allowed_but_never_updated(self):
        value = plan()
        value['resource_changes'].append({'type': 'oci_core_vcn', 'mode': 'managed',
            'provider_name': PROVIDER, 'change': {'actions': ['create'], 'after': {}}})
        validate_plan(value, 'compartment')
        value['resource_changes'][1]['change']['actions'] = ['update']
        with self.assertRaises(Blocked):
            validate_plan(value, 'compartment')

    def test_capacity_classification(self):
        self.assertTrue(capacity_only(['Error: 500-InternalError, Out of host capacity']))
        self.assertFalse(capacity_only(['Error: quota exceeded']))
        self.assertFalse(capacity_only(['out of host capacity', 'Error: invalid configuration']))
        self.assertFalse(capacity_only(['Error: unknown failure', 'Out of host capacity']))
        self.assertFalse(capacity_only(['Error: unknown failure']))

    def test_zip_digest_ignores_timestamps_but_detects_changes(self):
        original = stack_digest(archive(), {'ocpus': '2'}, '1.5')
        self.assertEqual(original, stack_digest(archive((2026, 1, 1, 0, 0, 0)), {'ocpus': '2'}, '1.5'))
        self.assertNotEqual(original, stack_digest(archive(), {'ocpus': '4'}, '1.5'))
        self.assertNotEqual(original, stack_digest(archive(content=b'changed'), {'ocpus': '2'}, '1.5'))
        self.assertNotEqual(original, stack_digest(archive(), {'ocpus': '2'}, '1.6'))

    def test_state_identity_and_foreign_instances(self):
        state = {'resources': [{'mode': 'managed', 'type': 'oci_core_instance', 'instances': [
            {'attributes': {'id': 'instance-id', 'display_name': 'vps-ampere-a1', 'shape': 'VM.Standard.A1.Flex'}}]}]}
        self.assertEqual(state_instance_ids(state), ['instance-id'])
        state['resources'][0]['instances'][0]['attributes']['display_name'] = 'amd'
        with self.assertRaises(Blocked):
            state_instance_ids(state)


if __name__ == '__main__':
    unittest.main()
