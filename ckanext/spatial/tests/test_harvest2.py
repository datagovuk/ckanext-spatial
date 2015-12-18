from nose.tools import assert_equal

try:
    from ckan.tests.helpers import reset_db
except ImportError:
    from ckan.new_tests.helpers import reset_db

from ckanext.spatial.tests.base import SpatialTestBase
from ckanext.harvest.tests.lib import run_harvest

from ckanext.spatial.harvesters.gemini import GeminiDocHarvester

import xml_file_server

# Start WAF-alike server we can test harvesting against it
xml_file_server.serve()


class TestHarvest(SpatialTestBase):
    @classmethod
    def setup_class(cls):
        reset_db()
        # SpatialTestBase sets up harvest & spatial models
        SpatialTestBase.setup_class()

    def test_simple_harvest(self):
        test_doc = 'gemini2.1/dataset1.xml'

        results_by_guid = run_harvest(
            url='http://localhost:%s/%s' % (xml_file_server.PORT, test_doc),
            harvester=GeminiDocHarvester())

        assert_equal(results_by_guid.keys(), ['test-dataset-1'])
        result = results_by_guid.values()[0]
        assert_equal(result['state'], 'COMPLETE')
        assert_equal(result['report_status'], 'added')
        assert_equal(result['errors'], [])

#    def test_nonGeographicDataset(self):
#        test_doc = 'gemini2.1/non-geographic.xml'
#
#        results_by_guid = run_harvest(
#            url='http://localhost:%s/%s' % (xml_file_server.PORT, test_doc),
#            harvester=GeminiDocHarvester())
#
#        import pdb; pdb.set_trace()
