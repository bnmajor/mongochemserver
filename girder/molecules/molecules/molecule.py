import cherrypy
import json
import os
import functools
import requests
from jsonpath_rw import parse

from girder.api.describe import Description, autoDescribeRoute
from girder.api.docs import addModel
from girder.api.rest import Resource
from girder.api.rest import RestException, loadmodel, getCurrentUser
from girder.api import access
from girder.constants import AccessType
from girder.constants import SortDir
from girder.constants import TerminalColor
from girder.constants import TokenScope
from girder.models.file import File
from girder.utility.model_importer import ModelImporter
from . import avogadro
from . import openbabel
from . import chemspider
from . import query
from . import semantic
from . import constants
from molecules.utilities import async_requests
from molecules.utilities.molecules import create_molecule
from molecules.utilities.pagination import parse_pagination_params
from molecules.utilities.pagination import search_results_dict

from molecules.models.geometry import Geometry as GeometryModel
from molecules.models.molecule import Molecule as MoleculeModel

class Molecule(Resource):
    output_formats_2d = ['smiles', 'inchi', 'inchikey']
    output_formats_3d = ['cml', 'xyz', 'sdf', 'cjson']
    output_formats = output_formats_2d + output_formats_3d

    input_formats = ['cml', 'xyz', 'sdf', 'cjson', 'json', 'log', 'nwchem', 'pdb', 'smi', 'smiles']
    mime_types = {
        'smiles': 'chemical/x-daylight-smiles',
        'inchi': 'chemical/x-inchi',
        'inchikey': 'text/plain',
        'cml': 'chemical/x-cml',
        'xyz': 'chemical/x-xyz',
        'sdf': 'chemical/x-mdl-sdfile',
        'cjson': 'application/json',
        'svg': 'image/svg+xml'
    }

    def __init__(self):
        super(Molecule, self).__init__()
        self.resourceName = 'molecules'
        self.route('GET', (), self.find)
        self.route('GET', ('inchikey', ':inchikey'), self.find_inchikey)
        self.route('GET', (':id', ':output_format'), self.get_format)
        self.route('GET', (':id', ), self.find_id)
        self.route('GET', (':id', 'svg'), self.get_svg)
        self.route('GET', ('search',), self.search)
        self.route('POST', (), self.create)
        self.route('DELETE', (':id',), self.delete)
        self.route('PATCH', (':id',), self.update)
        self.route('PATCH', (':id', 'notebooks'), self.add_notebooks)
        self.route('POST', ('conversions', ':output_format'), self.conversions)
        self.route('POST', (':id', '3d'), self.generate_3d_coords)

        # Methods for geometries
        self.route('GET', (':moleculeId', 'geometries'), self.find_geometries)
        self.route('GET', (':moleculeId', 'geometries', ':id'),
                   self.find_geometry)
        self.route('GET', (':moleculeId', 'geometries', ':id',
                           ':output_format'),
                   self.get_geometry_format)
        self.route('POST', (':moleculeId', 'geometries'), self.create_geometry)
        self.route('DELETE', (':moleculeId', 'geometries', ':id'),
                   self.delete_geometry)

    def _clean(self, doc, cjson=True):
        del doc['access']
        if 'sdf' in doc:
            del doc['sdf']
        if 'svg' in doc:
            del doc['svg']
        doc['_id'] = str(doc['_id'])
        if 'cjson' in doc:
            if cjson:
                if 'basisSet' in doc['cjson']:
                    del doc['cjson']['basisSet']
                if 'vibrations' in doc['cjson']:
                    del doc['cjson']['vibrations']
            else:
                del doc['cjson']

        return doc

    @access.public
    def find(self, params):
        return MoleculeModel().find_molecule(params)
    find.description = (
            Description('Find a molecule.')
            .param('name', 'The name of the molecule', paramType='query',
                   required=False)
            .param('inchi', 'The InChI of the molecule', paramType='query',
                   required=False)
            .param('inchikey', 'The InChI key of the molecule', paramType='query',
                   required=False)
            .param('smiles', 'The SMILES of the molecule', paramType='query',
                   required=False)
            .param('formula',
                   'The formula (using the "Hill Order") to search for',
                   paramType='query', required=False)
            .param('creatorId', 'The id of the user that created the molecule',
                   paramType='query', required=False)
            .param('queryString', 'The query string to use for this search '
                                  '(supercedes all other search parameters)',
                   paramType='query', required=False)
            .pagingParams(defaultSort='_id',
                          defaultSortDir=SortDir.DESCENDING,
                          defaultLimit=25)
            .errorResponse())

    @access.public
    def find_inchikey(self, inchikey, params):
        mol = MoleculeModel().find_inchikey(inchikey)
        if not mol:
            raise RestException('Molecule not found.', code=404)
        return self._clean(mol)
    find_inchikey.description = (
            Description('Find a molecule by InChI key.')
            .param('inchikey', 'The InChI key of the molecule', paramType='path')
            .errorResponse()
           .errorResponse('Molecule not found.', 404))

    @access.public
    def find_id(self, id, params):
        mol = MoleculeModel().load(id, level=AccessType.READ, user=getCurrentUser())
        if not mol:
            raise RestException('Molecule not found.', code=404)
        cjson = True
        cjsonParam = params.get('cjson')
        if cjsonParam is not None:
            cjson = cjsonParam.lower() == 'true'
        return self._clean(mol, cjson)
    find_id.description = (
        Description('Get a specific molecule by id')
        .param('id', 'The id of the molecule', paramType='path')
        .param('cjson', 'Attach the cjson data of the molecule to the response (Default: true)', paramType='query', required=False)
    )

    @access.user(scope=TokenScope.DATA_WRITE)
    def create(self, params):
        body = self.getBodyJson()
        user = self.getCurrentUser()
        public = body.get('public', True)
        gen3d = body.get('generate3D', True)
        gen3d_forcefield = body.get('gen3dForcefield', 'mmff94')
        gen3d_steps = body.get('gen3dSteps', 100)
        provenance = body.get('provenance', 'uploaded by user')
        mol = None
        if 'fileId' in body:
            file_id = body['fileId']
            file = ModelImporter.model('file').load(file_id, user=user)
            parts = file['name'].split('.')
            input_format = parts[-1]
            name = '.'.join(parts[:-1])

            if input_format not in Molecule.input_formats:
                raise RestException('Input format not supported.', code=400)

            with File().open(file) as f:
                data_str = f.read().decode()

            mol = create_molecule(data_str, input_format, user, public, gen3d,
                                  provenance, gen3d_forcefield, gen3d_steps, body)
        elif 'inchi' in body:
            input_format = 'inchi'
            data = body['inchi']
            if not data.startswith('InChI='):
                data = 'InChI=' + data

            mol = create_molecule(data, input_format, user, public, gen3d,
                                  provenance, gen3d_forcefield, gen3d_steps, body)

        for key in body:
            if key in Molecule.input_formats:
                input_format = key
                data = body[input_format]
                # Convert to str if necessary
                if isinstance(data, dict):
                    data = json.dumps(data)
                mol = create_molecule(data, input_format, user, public, gen3d, provenance,
                                      gen3d_forcefield, gen3d_steps, body)
                break

        if not mol:
            raise RestException('Invalid request', code=400)

        return self._clean(mol)

    addModel('Molecule', 'MoleculeParams', {
        "id": "MoleculeParams",
        "required": ["name", "inchi"],
        "properties": {
            "name": {"type": "string", "description": "The common name of the molecule"},
            "inchi": {"type": "string", "description": "The InChI of the molecule."}
        }
    })
    create.description = (
        Description('Create a molecule')
        .param(
            'body',
            'The molecule to be added to the database.',
            dataType='MoleculeParams',
            required=True, paramType='body')
        .errorResponse('Input format not supported.', code=400))

    @access.user(scope=TokenScope.DATA_WRITE)
    def delete(self, id, params):
        user = self.getCurrentUser()
        mol = MoleculeModel().load(id, user=user, level=AccessType.WRITE)

        if not mol:
            raise RestException('Molecule not found.', code=404)

        return MoleculeModel().remove(mol)

    delete.description = (
            Description('Delete a molecule by id.')
            .param('id', 'The id of the molecule', paramType='path')
            .errorResponse()
            .errorResponse('Molecule not found.', 404))

    @access.user(scope=TokenScope.DATA_WRITE)
    def update(self, id, params):
        user = self.getCurrentUser()

        mol = MoleculeModel().load(id, user=user, level=AccessType.WRITE)

        if not mol:
            raise RestException('Molecule not found.', code=404)

        body = self.getBodyJson()

        query = {
            '_id': mol['_id']
        }

        updates = {
            '$set': {},
            '$addToSet': {}
        }

        if 'name' in body:
            updates['$set']['name'] = body['name']

        if 'logs' in body:
            updates['$addToSet']['logs'] = body['logs']

        # Remove unused keys
        updates = {k: v for k, v in updates.items() if v}

        super(MoleculeModel, MoleculeModel()).update(query, updates)

        # Reload the molecule
        mol = MoleculeModel().load(id, user=user)

        return self._clean(mol)
    addModel('Molecule', 'UpdateMoleculeParams', {
        "id": "UpdateMoleculeParams",
        "properties": {
            "logs": {"type": "array", "description": "List of Girder file ids"}
        }
    })
    update.description = (
            Description('Update a molecule by id.')
            .param('id', 'The id of the molecule', paramType='path')
            .param(
            'body',
            'The update to the molecule.',
            dataType='UpdateMoleculeParams',
            required=True, paramType='body')
            .errorResponse('Molecule not found.', 404))

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
        Description('Add notebooks ( file ids ) to molecule.')
        .modelParam('id', 'The molecule id',
                    model=MoleculeModel, destName='molecule',
                    force=True, paramType='path')
        .jsonParam('notebooks', 'List of notebooks', required=True, paramType='body')
    )
    def add_notebooks(self, molecule, notebooks):
        notebooks = notebooks.get('notebooks')
        if notebooks is not None:
            MoleculeModel().add_notebooks(molecule, notebooks)

    @access.user(scope=TokenScope.DATA_READ)
    def conversions(self, output_format, params):
        user = self.getCurrentUser()

        if output_format not in Molecule.output_formats:
            raise RestException('Output output_format not supported.', code=404)

        body = self.getBodyJson()

        if 'fileId' not in body and 'cjson' not in body:
            raise RestException('Invalid request body.', code=400)

        if 'fileId' in body:
            file_id = body['fileId']
            file = ModelImporter.model('file').load(file_id, user=user)
            input_format = file['name'].split('.')[-1]

            if input_format not in Molecule.input_formats:
                raise RestException('Input format not supported.', code=400)

            with File().open(file) as f:
                data_str = f.read().decode()
        else:
            input_format = 'cjson'
            data_str = json.dumps(body['cjson'])

        if output_format.startswith('inchi'):
            atom_count = 0
            if input_format == 'pdb':
                props = openbabel.properties(data_str, input_format)
                atom_count = props['atomCount']
            else:
                atom_count = int(avogadro.atom_count(data_str, input_format))

            if atom_count > 1024:
                raise RestException('Unable to generate InChI, molecule has more than 1024 atoms.', code=400)

            if input_format == 'pdb':
                (inchi, inchikey) = openbabel.to_inchi(data_str, input_format)
            else:
                sdf = avogadro.convert_str(data_str, input_format, 'sdf')
                (inchi, inchikey) = openbabel.to_inchi(sdf, 'sdf')

            if output_format == 'inchi':
                return inchi
            else:
                return inchikey

        else:
            output = ''
            mime = 'text/plain'
            if input_format == 'pdb':
                (output, mime) = openbabel.convert_str(data_str, input_format,
                                                       output_format)
            else:
                output = avogadro.convert_str(data_str, input_format,
                                              output_format)

            def stream():
                cherrypy.response.headers['Content-Type'] = mime
                yield output

            return stream

    addModel('Molecule', 'ConversionParams', {
        "id": "ConversionParams",
        "properties": {
            "fileId": {"type": "string", "description": "Girder file id to do conversion on"}
        }
    })
    conversions.description = (
            Description('Update a molecule by id.')
            .param('output_format', 'The format to convert to', paramType='path')
            .param(
            'body',
            'Details of molecule data to perform conversion on',
            dataType='ConversionParams',
            required=True, paramType='body')
            .errorResponse('Output format not supported.', 404)
            .errorResponse('File not found.', 404)
            .errorResponse('Invalid request body.', 400)
            .errorResponse('Input format not supported.', code=400))

    @access.public
    def get_format(self, id, output_format, params):
        # For now will for force load ( i.e. ignore access control )
        # This will change when we have access controls.
        molecule = MoleculeModel().load(id, force=True)

        if output_format not in Molecule.output_formats:
            raise RestException('Format not supported.', code=400)

        if output_format in Molecule.output_formats_3d:
            # If it is a 3d output format, cjson is required
            if 'cjson' not in molecule:
                # Returning None implies that there are no 3D coordinates
                return

            data = json.dumps(molecule['cjson'])
            if output_format != 'cjson':
                data = avogadro.convert_str(data, 'cjson', output_format)
        else:
            # Right now, all 2d output formats are stored in the molecule
            data = molecule[output_format]

        def stream():
            cherrypy.response.headers['Content-Type'] = Molecule.mime_types[output_format]
            yield data

        return stream

    get_format.description = (
            Description('Get molecule in particular format.')
            .param('id', 'The id of the molecule', paramType='path')
            .param('output_format', 'The format to convert to', paramType='path')
            .errorResponse('Output format not supported.', 400)
            .errorResponse('Molecule does not have 3D coordinates.', 404))

    @access.public
    @autoDescribeRoute(
            Description('Get an SVG representation of a molecule.')
            .param('id', 'The id of the molecule', paramType='path')
            .errorResponse('Molecule not found.', 404)
            .errorResponse('Molecule does not have SVG data.', 404))
    def get_svg(self, id):
        # For now will for force load ( i.e. ignore access control )
        # This will change when we have access controls.
        mol = MoleculeModel().load(id, force=True)

        if not mol:
            raise RestException('Molecule not found.', code=404)

        if 'svg' not in mol:
            raise RestException('Molecule does not have SVG data.', code=404)

        data = mol['svg']

        cherrypy.response.headers['Content-Type'] = Molecule.mime_types['svg']

        def stream():
            yield data.encode()

        return stream

    @access.public
    def search(self, params):
        limit, offset, sort = parse_pagination_params(params)

        query_string = params.get('q')
        formula = params.get('formula')
        cactus = params.get('cactus')
        if query_string is None and formula is None and cactus is None:
            raise RestException('Either \'q\', \'formula\' or \'cactus\' is required.')

        if query_string is not None:
            try:
                mongo_query = query.to_mongo_query(query_string)
            except query.InvalidQuery:
                raise RestException('Invalid query', 400)

            fields = [
              'inchikey',
              'smiles',
              'properties',
              'name'
            ]
            cursor = MoleculeModel().find(query=mongo_query, fields=fields,
                                          limit=limit, offset=offset,
                                          sort=sort)
            mols = [x for x in cursor]
            num_matches = cursor.collection.count_documents(mongo_query)

            return search_results_dict(mols, num_matches, limit, offset, sort)

        elif formula:
            # Search using formula
            return MoleculeModel().find_molecule(params)

        elif cactus:
            if getCurrentUser() is None:
                raise RestException('Must be logged in to search with cactus.')

            # Disable cert verification for now
            # TODO Ensure we have the right root certs so this just works.
            r = requests.get('https://cactus.nci.nih.gov/chemical/structure/%s/file?format=sdf' % cactus, verify=False)

            if r.status_code == 404:
                return []
            else:
                r.raise_for_status()

            sdf_data = r.content.decode('utf8')
            provenance = 'cactus: ' + cactus
            mol = create_molecule(sdf_data, 'sdf', getCurrentUser(), True,
                                  provenance=provenance)

            return search_results_dict([mol], 1, limit, offset, sort)


    search.description = (
            Description('Search for molecules using a query string, formula, or cactus')
            .param('q', 'The query string to use for this search', paramType='query', required=False)
            .param('formula', 'The formula (using the "Hill Order") to search for', paramType='query', required=False)
            .param('cactus', 'The identifier to pass to cactus', paramType='query', required=False)
            .pagingParams(defaultSort='_id',
                          defaultSortDir=SortDir.DESCENDING,
                          defaultLimit=25))

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
            Description('Generate 3D coordinates for a molecule.')
            .modelParam('id', 'The id of the molecule', destName='mol',
                        level=AccessType.WRITE, model=MoleculeModel)
            .param('forcefield', 'The force field to use', default='mmff94',
                   strip=True)
            .param('steps', 'The number of optimization steps', default='100',
                   dataType='integer')
            .errorResponse('Molecule not found.', 404)
    )
    def generate_3d_coords(self, mol, forcefield, steps):
        """Generate 3D coords if not present and not being generated"""

        if (MoleculeModel().has_3d_coords(mol) or
                mol.get('generating_3d_coords', False)):
            return self._clean(mol)

        try:
            steps = int(steps)
        except ValueError:
            raise RestException(str(steps) + ' is not a number')

        user = self.getCurrentUser()

        async_requests.schedule_3d_coords_gen(mol, user,
                                              gen3d_forcefield=forcefield,
                                              gen3d_steps=steps)
        return self._clean(mol)

    @access.public
    @autoDescribeRoute(
        Description('Find geometries of a given molecule.')
        .param('moleculeId', 'The id of the parent molecule.')
        .pagingParams(defaultSort='_id',
                      defaultSortDir=SortDir.DESCENDING,
                      defaultLimit=25)
    )
    def find_geometries(self, moleculeId, limit, offset, sort):
        paging_params = {
            'limit': limit,
            'offset': offset,
            'sort': sort[0][0],
            'sortdir': sort[0][1]
        }
        user = getCurrentUser()
        return GeometryModel().find_geometries(moleculeId, user, paging_params)

    @access.public
    @autoDescribeRoute(
        Description('Find a geometry of a given molecule.')
        .param('moleculeId', 'The id of the parent molecule.')
        .param('id', 'The id of the geometry.')
    )
    def find_geometry(self, moleculeId, id):
        user = getCurrentUser()
        geometry = GeometryModel().load(id, level=AccessType.READ, user=user)

        if not geometry:
            raise RestException('Geometry not found.', code=404)

        return self._clean(geometry)

    @access.public
    @autoDescribeRoute(
        Description('Get a geometry in a specified format.')
        .param('moleculeId', 'The id of the parent molecule.')
        .param('id', 'The id of the geometry to be used.')
        .param('output_format', 'The output format of the geometry.')
        .errorResponse('Format not supported.')
        .errorResponse('Geometry not found.', 404)
    )
    def get_geometry_format(self, moleculeId, id, output_format):

        if output_format not in Molecule.output_formats:
            raise RestException('Format not supported.')

        if output_format in Molecule.output_formats_2d:
            # It's just smiles or inchi, call the general end point
            return self.get_format(moleculeId, output_format, None)

        user = getCurrentUser()
        geometry = GeometryModel().load(id, level=AccessType.READ, user=user)

        if not geometry:
            raise RestException('Geometry not found.', code=404)

        data = json.dumps(geometry['cjson'])
        if output_format != 'cjson':
            data = avogadro.convert_str(data, 'cjson', output_format)

        def stream():
            cherrypy.response.headers['Content-Type'] = (
                Molecule.mime_types[output_format]
            )
            yield data

        return stream

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
        Description('Create a geometry.')
        .param('moleculeId', 'The id of the parent molecule.')
        .jsonParam('cjson', 'The chemical json of the geometry.',
                   paramType='body')
        .param('provenanceType', 'The type of provenance.', required=False)
        .param('provenanceId', 'The ID of the provnenace.', required=False)
    )
    def create_geometry(self, moleculeId, cjson, provenanceType, provenanceId):
        user = getCurrentUser()

        if provenanceType is None:
            provenanceType = 'user uploaded'

        geometry = GeometryModel().create(user, moleculeId, cjson,
                                          provenanceType, provenanceId)
        return self._clean(geometry)

    @access.user(scope=TokenScope.DATA_WRITE)
    @autoDescribeRoute(
        Description('Delete a geometry.')
        .param('moleculeId', 'The id of the parent molecule.')
        .param('id', 'The id of the geometry to be deleted.')
        .errorResponse('Geometry not found.', 404)
    )
    def delete_geometry(self, moleculeId, id):
        user = self.getCurrentUser()
        geometry = GeometryModel().load(id, user=user, level=AccessType.WRITE)

        if not geometry:
            raise RestException('Geometry not found.', code=404)

        return GeometryModel().remove(geometry)
