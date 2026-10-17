import json
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import oci

from scripts.oracle_api import Oracle
from scripts.safety import Blocked
from tests.test_safety import archive, plan


class OracleAdapterTests(unittest.TestCase):
    def oracle(self):
        oracle = Oracle.__new__(Oracle)
        oracle.tenancy = 'tenancy'
        oracle.compartment_id = 'compartment'
        oracle.stack_id = 'stack'
        oracle.rm = Mock(spec=oci.resource_manager.ResourceManagerClient)
        oracle.compute = Mock(spec=oci.core.ComputeClient)
        oracle.block = Mock(spec=oci.core.BlockstorageClient)
        oracle.identity = Mock(spec=oci.identity.IdentityClient)
        oracle.limits = Mock(spec=oci.limits.LimitsClient)
        return oracle

    def test_inventory_covers_nested_compartments_and_unattached_volumes(self):
        oracle = self.oracle()
        oracle.identity.list_region_subscriptions.return_value = oci.response.Response(200, {}, [
            Obj(region_name='sa-saopaulo-1', is_home_region=True)], None)
        oracle.identity.list_compartments.return_value = oci.response.Response(200, {}, [
            Obj(id='child', lifecycle_state='ACTIVE')], None)
        oracle.identity.list_availability_domains.return_value = oci.response.Response(200, {}, [Obj(name='AD-1')], None)
        oracle.compute.list_instances.return_value = oci.response.Response(200, {}, [], None)
        # Both tenancy root and child are counted, independently of attachments.
        oracle.block.list_volumes.return_value = oci.response.Response(200, {}, [
            Obj(lifecycle_state='AVAILABLE', size_in_gbs=25)], None)
        oracle.block.list_boot_volumes.return_value = oci.response.Response(200, {}, [
            Obj(lifecycle_state='AVAILABLE', size_in_gbs=50)], None)
        result = oracle.inventory()
        self.assertEqual(result['storage_gb'], 150)
        oracle.identity.list_compartments.assert_called_once_with(
            'tenancy', compartment_id_in_subtree=True, access_level='ANY')
        self.assertEqual(oracle.block.list_boot_volumes.call_count, 2)
        oracle.block.list_boot_volumes.assert_any_call(availability_domain='AD-1', compartment_id='child')

    def test_limits_and_image_are_validated_without_mutating(self):
        oracle = self.oracle()
        oracle.compute.get_image.return_value = Obj(data=Obj(
            operating_system='Oracle Linux', operating_system_version='9.6', compartment_id=None,
            listing_type='NONE', launch_mode='PARAVIRTUALIZED', size_in_mbs=47694))
        oracle.limits.list_limit_values.return_value = oci.response.Response(200, {}, [
            Obj(name=name, scope_type='AD', availability_domain='AD-1')
            for name in ('standard-a1-core-count', 'standard-a1-memory-count')], None)
        oracle.limits.get_resource_availability.return_value = Obj(data=Obj(available=24))
        inventory = {'storage_gb': 150, 'a1_ocpus': 0, 'a1_memory_gb': 0, 'targets': []}
        oracle.validate_capacity(plan()['resource_changes'][0]['change']['after'], inventory)
        self.assertEqual(oracle.limits.get_resource_availability.call_count, 4)
        oracle.limits.get_resource_availability.assert_any_call(
            'compute', 'standard-a1-core-count', 'tenancy', availability_domain='AD-1')
        oracle.limits.get_resource_availability.assert_any_call(
            'compute', 'standard-a1-memory-count', 'compartment', availability_domain='AD-1')
        oracle.rm.create_job.assert_not_called()

    def test_current_disk_overflow_and_marketplace_image_refused(self):
        oracle = self.oracle()
        inventory = {'storage_gb': 151, 'a1_ocpus': 0, 'a1_memory_gb': 0, 'targets': []}
        with self.assertRaises(Blocked):
            oracle.validate_capacity(plan()['resource_changes'][0]['change']['after'], inventory)
        oracle.compute.get_image.assert_not_called()
        inventory['storage_gb'] = 150
        oracle.compute.get_image.return_value = Obj(data=Obj(
            operating_system='Oracle Linux', operating_system_version='9', compartment_id='publisher',
            listing_type='COMMUNITY', launch_mode='PARAVIRTUALIZED', size_in_mbs=47694))
        with self.assertRaises(Blocked):
            oracle.validate_capacity(plan()['resource_changes'][0]['change']['after'], inventory)

    def test_sdk_job_models_reference_exact_plan_and_idempotency_token(self):
        oracle = self.oracle()
        oracle.rm.create_job.return_value = Obj(data='job')
        oracle.create('APPLY', 'request-name', 'stable-token', 'approved-plan')
        request = oracle.rm.create_job.call_args.args[0]
        self.assertIsInstance(request, oci.resource_manager.models.CreateJobDetails)
        self.assertEqual(request.job_operation_details.execution_plan_strategy, 'FROM_PLAN_JOB_ID')
        self.assertEqual(request.job_operation_details.execution_plan_job_id, 'approved-plan')
        self.assertEqual(oracle.rm.create_job.call_args.kwargs['opc_retry_token'], 'stable-token')
        with self.assertRaises(Blocked):
            oracle.create('APPLY', 'name', 'token')

    def test_digest_reloads_variables_and_rejects_custom_providers(self):
        oracle = self.oracle()
        stack = Obj(config_source=Obj(config_source_type='ZIP_UPLOAD'), variables={'cpu': '2'},
                    terraform_version='1.5', custom_terraform_provider=None,
                    is_third_party_provider_experience_enabled=False)
        oracle.rm.get_stack.return_value = Obj(data=stack)
        response = Mock()
        response.data.raw.stream.side_effect = lambda *a, **kw: iter([archive()])
        oracle.rm.get_stack_tf_config.return_value = response
        before = oracle.digest()
        stack.variables = {'cpu': '4'}
        self.assertNotEqual(before, oracle.digest())
        # This capability flag is enabled by modern stacks, including OCI-only ones.
        stack.is_third_party_provider_experience_enabled = True
        oracle.digest()
        stack.custom_terraform_provider = oci.resource_manager.models.CustomTerraformProvider()
        oracle.digest()
        stack.custom_terraform_provider = oci.resource_manager.models.CustomTerraformProvider(bucket_name='custom')
        with self.assertRaises(Blocked):
            oracle.digest()


if __name__ == '__main__':
    unittest.main()
