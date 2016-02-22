import os
import re
from logging import getLogger
from pylons import config
from pylons.i18n import _

from ckan.lib.helpers import json

from ckan import plugins as p

from ckan.logic import ValidationError

import html


def check_geoalchemy_requirement():
    '''Checks if a suitable geoalchemy version installed

       Checks if geoalchemy2 is present when using CKAN >= 2.3, and raises
       an ImportError otherwise so users can upgrade manually.
    '''

    msg = ('This version of ckanext-spatial requires {0}. ' +
           'Please install it by running `pip install {0}`.\n' +
           'For more details see the "Troubleshooting" section of the ' +
           'install documentation')

    if p.toolkit.check_ckan_version(min_version='2.3'):
        try:
            import geoalchemy2
        except ImportError:
            raise ImportError(msg.format('geoalchemy2'))
    else:
        try:
            import geoalchemy
        except ImportError:
            raise ImportError(msg.format('geoalchemy'))


check_geoalchemy_requirement()

log = getLogger(__name__)


def package_error_summary(error_dict):
    ''' Do some i18n stuff on the error_dict keys '''

    def prettify(field_name):
        field_name = re.sub('(?<!\w)[Uu]rl(?!\w)', 'URL',
                            field_name.replace('_', ' ').capitalize())
        return _(field_name.replace('_', ' '))

    summary = {}
    for key, error in error_dict.iteritems():
        if key == 'resources':
            summary[_('Resources')] = _('Package resource(s) invalid')
        elif key == 'extras':
            summary[_('Extras')] = _('Missing Value')
        elif key == 'extras_validation':
            summary[_('Extras')] = error[0]
        else:
            summary[_(prettify(key))] = error[0]
    return summary

class SpatialMetadata(p.SingletonPlugin):

    p.implements(p.IPackageController, inherit=True)
    p.implements(p.IConfigurable, inherit=True)
    p.implements(p.IRoutes, inherit=True)

    def configure(self, config):
        from ckanext.spatial.model.package_extent import setup as setup_model
        if not config.get('ckan.spatial.testing',False):
            setup_model()

    def create(self, package):
        self.check_spatial_extra(package)

    def edit(self, package):
        self.check_spatial_extra(package)

    def check_spatial_extra(self,package):
        '''
        For a given package, looks at the spatial extent (as given in the
        extra "spatial" in GeoJSON format) and records it in PostGIS.
        '''
        from ckanext.spatial.lib import save_package_extent

        if not package.id:
            log.warning('Couldn\'t store spatial extent because no id was provided for the package')
            return

        # TODO: deleted extra
        for extra in package.extras_list:
            if extra.key == 'spatial':
                if extra.state == 'active':
                    try:
                        log.debug('Received: %r' % extra.value)
                        geometry = json.loads(extra.value)
                    except ValueError,e:
                        error_dict = {'spatial':[u'Error decoding JSON object: %s' % str(e)]}
                        raise ValidationError(error_dict, error_summary=package_error_summary(error_dict))
                    except TypeError,e:
                        error_dict = {'spatial':[u'Error decoding JSON object: %s' % str(e)]}
                        raise ValidationError(error_dict, error_summary=package_error_summary(error_dict))

                    try:
                        save_package_extent(package.id,geometry)

                    except ValueError,e:
                        error_dict = {'spatial':[u'Error creating geometry: %s' % str(e)]}
                        raise ValidationError(error_dict, error_summary=package_error_summary(error_dict))
                    except Exception, e:
                        if bool(os.getenv('DEBUG')):
                            raise
                        error_dict = {'spatial':[u'Error: %s' % str(e)]}
                        raise ValidationError(error_dict, error_summary=package_error_summary(error_dict))

                elif extra.state == 'deleted':
                    # Delete extent from table
                    save_package_extent(package.id,None)

                break


    def delete(self, package):
        from ckanext.spatial.lib import save_package_extent
        save_package_extent(package.id,None)

    def before_map(self, route_map):
        view_controller = 'ckanext.spatial.controllers.view:ViewController'
        route_map.connect('/harvest/gemini-object/:id', controller=view_controller,
                          action='harvest_metadata_html')

        return route_map

    def after_map(self, route_map):
        return route_map

class SpatialQuery(p.SingletonPlugin):

    p.implements(p.IRoutes, inherit=True)
    p.implements(p.IPackageController, inherit=True)
    p.implements(p.IConfigurable, inherit=True)

    search_backend = None

    def configure(self, config):

        self.search_backend = 'postgis'

    def before_map(self, map):

        map.connect('api_spatial_query', '/api/2/search/{register:dataset|package}/geo',
            controller='ckanext.spatial.controllers.api:ApiController',
            action='spatial_query')
        return map

    def before_search(self,search_params):
        from ckan.lib.search import SearchQueryError
        from ckanext.spatial.lib import validate_bbox, bbox_query, bbox_query_ordered
        if 'extras' in search_params and 'ext_bbox' in search_params['extras'] \
            and search_params['extras']['ext_bbox']:

            bbox = validate_bbox(search_params['extras']['ext_bbox'])
            if not bbox:
                raise SearchQueryError('Wrong bounding box provided')
            if search_params.get('sort') == 'spatial desc':
                if search_params.get('q') or search_params.get('fq'):
                    raise SearchQueryError('Spatial ranking cannot be mixed with other search parameters')
                    # ...because it is too inefficient to use SOLR to filter
                    # results and return the entire set to this class and
                    # after_search do the sorting and paging.
                extents = bbox_query_ordered(bbox)
                are_no_results = not extents
                search_params['extras']['ext_rows'] = search_params.get('rows', 50)
                search_params['extras']['ext_start'] = search_params.get('start', 0)
                # this SOLR query needs to return no actual results since
                # they are in the wrong order anyway. We just need this SOLR
                # query to get the count and facet counts.
                rows = 0
                search_params['sort'] = None # SOLR should not sort.
                # Store the rankings of the results for this page, so for
                # after_search to construct the correctly sorted results
                rows = search_params['extras']['ext_rows'] = search_params.get('rows', 50)
                start = search_params['extras']['ext_start'] = search_params.get('start', 0)
                search_params['extras']['ext_spatial'] = [
                    (extent.package_id, extent.spatial_ranking) \
                    for extent in extents[start:start+rows]]
            else:
                extents = bbox_query(bbox)
                are_no_results = extents.count() == 0

            if are_no_results:
                # We don't need to perform the search
                search_params['abort_search'] = True
            else:
                # We'll perform the existing search but also filtering by the ids
                # of datasets within the bbox
                bbox_query_ids = [extent.package_id for extent in extents]

                q = search_params.get('q','').strip() or '""'
                new_q = '%s AND ' % q if q else ''
                new_q += '(%s)' % ' OR '.join(['id:%s' % id for id in bbox_query_ids])

                search_params['q'] = new_q

        return search_params

    def after_search(self, search_results, search_params):
        from ckan.lib.search import SearchError, PackageSearchQuery

        if search_params.get('extras', {}).get('ext_spatial'):
            # Apply the spatial sort
            querier = PackageSearchQuery()
            pkgs = []
            for package_id, spatial_ranking in search_params['extras']['ext_spatial']:
                # get package from SOLR
                try:
                    pkg = querier.get_index(package_id)['data_dict']
                    pkgs.append(json.loads(pkg))
                except SearchError:
                    # if this dataset is not in the index, then the error goes
                    # in the log to attract attention of sysadmins, but we
                    # still want to try and return the other results, as we
                    # would for a normal CKAN text search.
                    pass
            search_results['results'] = pkgs
        return search_results


class CatalogueServiceWeb(p.SingletonPlugin):
    p.implements(p.IConfigurable)
    p.implements(p.IRoutes)

    def configure(self, config):
        config.setdefault("cswservice.title", "Untitled Service - set cswservice.title in config")
        config.setdefault("cswservice.abstract", "Unspecified service description - set cswservice.abstract in config")
        config.setdefault("cswservice.keywords", "")
        config.setdefault("cswservice.keyword_type", "theme")
        config.setdefault("cswservice.provider_name", "Unnamed provider - set cswservice.provider_name in config")
        config.setdefault("cswservice.contact_name", "No contact - set cswservice.contact_name in config")
        config.setdefault("cswservice.contact_position", "")
        config.setdefault("cswservice.contact_voice", "")
        config.setdefault("cswservice.contact_fax", "")
        config.setdefault("cswservice.contact_address", "")
        config.setdefault("cswservice.contact_city", "")
        config.setdefault("cswservice.contact_region", "")
        config.setdefault("cswservice.contact_pcode", "")
        config.setdefault("cswservice.contact_country", "")
        config.setdefault("cswservice.contact_email", "")
        config.setdefault("cswservice.contact_hours", "")
        config.setdefault("cswservice.contact_instructions", "")
        config.setdefault("cswservice.contact_role", "")

        config["cswservice.rndlog_threshold"] = float(config.get("cswservice.rndlog_threshold", "0.01"))

    def before_map(self, route_map):
        c = "ckanext.spatial.controllers.csw:CatalogueServiceWebController"
        route_map.connect("/csw", controller=c, action="dispatch_get",
                          conditions={"method": ["GET"]})
        route_map.connect("/csw", controller=c, action="dispatch_post",
                          conditions={"method": ["POST"]})

        return route_map

    def after_map(self, route_map):
        return route_map

class HarvestMetadataApi(p.SingletonPlugin):
    '''
    Harvest Metadata API
    (previously called "InspireApi")

    A way for a user to view the harvested metadata XML, either as a raw file or
    styled to view in a web browser.
    '''
    p.implements(p.IRoutes)
    p.implements(p.IConfigurer, inherit=True)

    def before_map(self, route_map):
        harvest_metadata_api_controller = "ckanext.spatial.controllers.api:HarvestMetadataApiController"

        route_map.connect("/api/2/rest/harvestobject/:id/xml", controller=harvest_metadata_api_controller,
                          action="display_xml")
        route_map.redirect('/api/2/rest/harvestobject/{id:[^\/]*}/html', '/harvest/gemini-object/{id}')
        return route_map

    def after_map(self, route_map):
        return route_map

    def update_config(self, config):
        here = os.path.dirname(__file__)

        template_dir = os.path.join(here, 'templates')

        if config.get('extra_template_paths'):
            config['extra_template_paths'] += ','+template_dir
        else:
            config['extra_template_paths'] = template_dir
