from flask import Flask, jsonify, request, Response

import openbabel_api as openbabel

app = Flask(__name__)


@app.route('/convert/<output_format>', methods=['POST'])
def convert(output_format):
    """Convert molecule data from one format to another via OpenBabel

    The output format is specified in the path. The input format and
    the data are specified in the body (in json format) as the keys
    "format" and "data", respectively.

    Some options may also be specified in the json body. The following
    options will be used in all cases other than the special ones:

        gen3d (bool): should we generate 3d coordinates?
        addHydrogens (bool): should we add hydrogens?
        perceiveBonds (bool): should we perceive bonds?
        outOptions (dict): what extra output options are there?

    Special cases are:
        output_format:
            svg: returns the SVG
            smi: returns canonical smiles
            inchi: returns json containing "inchi" and "inchikey"

    Curl example:
    curl -X POST 'http://localhost:5000/convert/inchi' \
      -H "Content-Type: application/json" \
      -d '{"format": "smiles", "data": "CCO"}'
    """
    json_data = request.get_json()
    input_format = json_data['format']
    data = json_data['data']

    # Treat special cases with special functions
    out_lower = output_format.lower()
    if out_lower == 'svg':
        data, mime = openbabel.to_svg(data, input_format)
    elif out_lower in ['smiles', 'smi']:
        data, mime = openbabel.to_smiles(data, input_format)
    elif out_lower == 'inchi':
        inchi, inchikey = openbabel.to_inchi(data, input_format)
        d = {
            'inchi': inchi,
            'inchikey': inchikey
        }
        return jsonify(d)
    else:
        # Check for a few specific arguments
        gen3d = json_data.get('gen3d', False)
        add_hydrogens = json_data.get('addHydrogens', False)
        perceive_bonds = json_data.get('perceiveBonds', False)
        out_options = json_data.get('outOptions', {})
        gen3d_forcefield = json_data.get('gen3dForcefield', 'mmff94')
        gen3d_steps = json_data.get('gen3dSteps', 100)

        data, mime = openbabel.convert_str(data, input_format, output_format,
                                           gen3d=gen3d,
                                           add_hydrogens=add_hydrogens,
                                           perceive_bonds=perceive_bonds,
                                           out_options=out_options,
                                           gen3d_forcefield=gen3d_forcefield,
                                           gen3d_steps=gen3d_steps)
    return Response(data, mimetype=mime)


@app.route('/properties', methods=['POST'])
def properties():
    """Get the properties of a molecule

    The input format and the data are specified in the body (in json
    format) as the keys "format" and "data", respectively.

    There is additionally one option, listed below, that can be added
    as a key to the json.

        addHydrogens (bool): should we add hydrogens?

    Returns json containing the atom count, formula, heavy atom count,
    mass, and spaced formula.

    Curl example:
    curl -X POST 'http://localhost:5000/properties' \
      -H "Content-Type: application/json" \
      -d '{"format": "smiles", "data": "CCO"}'
    """
    json_data = request.get_json()
    input_format = json_data['format']
    data = json_data['data']
    add_hydrogens = json_data.get('addHydrogens', False)

    props = openbabel.properties(data, input_format, add_hydrogens)
    return jsonify(props)

if __name__ == '__main__':
    app.run(host='0.0.0.0')
