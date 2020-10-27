from rdflib import Dataset, Graph, Namespace, RDF, RDFS, OWL, XSD, Literal, URIRef
import converter.csvw as csvw
import os
import yaml
import datetime
import string
import logging
import iribaker
import urllib
import uuid
from jinja2 import Template
import rfc3987
import re
from hashlib import sha1

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
logger.addHandler(ch)

"""
Initialize a set of default namespaces from a configuration file (namespaces.yaml)
"""
# global namespaces
namespaces = {}
YAML_NAMESPACE_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'namespaces.yaml')


def init():
    """
    Initialize the module and assign namespaces to globals
    """
    # Read the file into a dictionary
    with open(YAML_NAMESPACE_FILE, 'r') as nsfile:
        global namespaces
        namespaces = yaml.load(nsfile, Loader=yaml.FullLoader)

    # Replace each value with a Namespace object for that value
    for prefix, uri in namespaces.items():
        if isinstance(prefix, str) and isinstance(uri, str):
            namespaces[prefix] = Namespace(uri)

    # Add all namespace prefixes to the globals dictionary (for exporting)
    for prefix, namespace in namespaces.items():
        globals()[prefix.upper()] = namespace

# Make sure the namespaces are initialized when the module is imported
init()



# TODO: put in class as it is part of Nanopublication 

def open_file_then_apply_git_hash(file_name):
    """
    Generates a Git-compatible hash for identifying (the current version of) the data
    """
    file_hash = sha1()
    file_size = 0

    try:
        file_size = os.path.getsize(file_name)
    except OSError as e:
        logger.error(f"Could not find the file: {file_name}\n")
        raise e

    git_specific_prefix = f"blob {file_size}\0"
    file_hash.update(git_specific_prefix.encode('utf-8'))
    with open(file_name, 'rb') as infile:
        for line in infile:
            file_hash.update(line)
    return file_hash.hexdigest()

# Part of Burstconverter + build_schema
def get_namespaces(base=None):
    """Return the global namespaces"""
    if base:
        namespaces['sdr'] = Namespace(str(base + '/'))
        namespaces['sdv'] = Namespace(str(base + '/vocab/'))
        with open(YAML_NAMESPACE_FILE, 'w') as outfile:
            yaml.dump(namespaces, outfile, default_flow_style=True)
    return namespaces

def validateTerm(term, headers):
    # IRIs have a URIRef type
    if type(term) == URIRef:
        iri = None
        template = Template(term)
        #E.g. http://example.com/{{jinja_statement}} --> http://example.com/None

        rendered_template = None
        try:
            rendered_template = template.render(**headers)
            #E.g. http://example.com/{csv_column_name} --> http://example.com/None
        except TypeError as e:
            # This could happen when LD concepts interact with Jinja concepts, e.g. {{ _row + 'some_string' }}
            # In that case we take the {{ }} out, and assume the template is fine
            # In the rare cases it isn't, the conversion will fail
            rendered_template = re.sub(r'/{{.+}}', '', str(term))

        try:
            potentially_valid_iri = rendered_template.format(**headers)
            iri = iribaker.to_iri(potentially_valid_iri)
            rfc3987.parse(iri, rule='IRI')
        except ValueError as e:
            logger.error(f"Found an invalid IRI: {iri}")
            raise e

def parse_value(value):
    if value == None:
        return value
    elif type(value) is csvw.Item:
        # See https://rdflib.readthedocs.io/en/stable/rdf_terms.html
        return str(value.identifier)
    else: # assuming value is a string or can be coerced as such (i.e. rdflib.term)
        return str(value)


class Nanopublication(Dataset):
    """
    A subclass of the rdflib Dataset class that comes pre-initialized with
    required Nanopublication graphs: np, pg, ag, pig, for nanopublication, provenance,
    assertion and publication info, respectively.

    NOTE: Will only work if the required namespaces are specified in namespaces.yaml and the init() function has been called
    """

    def __init__(self, file_name):
        """
        Initialize the graphs needed for the nanopublication
        """
        super().__init__()

        # Virtuoso does not accept BNodes as graph names
        self.default_context = Graph(store=self.store, identifier=URIRef(uuid.uuid4().urn))


        # Assign default namespace prefixes
        for prefix, namespace in namespaces.items():
            self.bind(prefix, namespace)

        # Get the current date and time (UTC)
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M")

        # Obtain a hash of the source file used for the conversion.
        # TODO: Get this directly from GitLab
        source_hash = open_file_then_apply_git_hash(file_name)

        # Shorten the source hash to 8 digits (similar to Github)
        short_hash = source_hash[:8]

        # Determine a 'hash_part' for all timestamped URIs generated through this procedure
        hash_part = f"{short_hash}/{timestamp}"

        # A URI that represents the version of the file being converted
        self.dataset_version_uri = SDR[source_hash]
        self.add((self.dataset_version_uri, SDV['path'], Literal(file_name, datatype=XSD.string)))
        self.add((self.dataset_version_uri, SDV['sha1_hash'], Literal(source_hash, datatype=XSD.string)))

        # ----
        # The nanopublication graph
        # ----
        name = (os.path.basename(file_name)).split('.')[0]
        self.uri = SDR[f"{name}/nanopublication/{hash_part}"]


        # The Nanopublication consists of three graphs
        assertion_graph_uri = SDR[f"{name}/assertion/{hash_part}"]
        provenance_graph_uri = SDR[f"{name}/provenance/{hash_part}"]
        pubinfo_graph_uri = SDR[f"{name}/pubinfo/{hash_part}"]

        self.ag = self.graph(assertion_graph_uri)
        self.pg = self.graph(provenance_graph_uri)
        self.pig = self.graph(pubinfo_graph_uri)

        # The nanopublication
        self.add((self.uri , RDF.type, NP['Nanopublication']))
        # The link to the assertion
        self.add((self.uri , NP['hasAssertion'], assertion_graph_uri))
        self.add((assertion_graph_uri, RDF.type, NP['Assertion']))
        # The link to the provenance graph
        self.add((self.uri , NP['hasProvenance'], provenance_graph_uri))
        self.add((provenance_graph_uri, RDF.type, NP['Provenance']))
        # The link to the publication info graph
        self.add((self.uri , NP['hasPublicationInfo'], pubinfo_graph_uri))
        self.add((pubinfo_graph_uri, RDF.type, NP['PublicationInfo']))

        # ----
        # The provenance graph
        # ----

        # Provenance information for the assertion graph (the data structure definition itself)
        self.pg.add((assertion_graph_uri, PROV['wasDerivedFrom'], self.dataset_version_uri))
        # self.pg.add((dataset_uri, PROV['wasDerivedFrom'], self.dataset_version_uri))
        self.pg.add((assertion_graph_uri, PROV['generatedAtTime'],
                     Literal(timestamp, datatype=XSD.dateTime)))

        # ----
        # The publication info graph
        # ----

        # The URI of the latest version of this converter
        # TODO: should point to the actual latest commit of this converter.
        # TODO: consider linking to this as the plan of some activity, rather than an activity itself.
        clariah_uri = URIRef('https://github.com/CLARIAH/wp4-converters')

        self.pig.add((self.uri, PROV['wasGeneratedBy'], clariah_uri))
        self.pig.add((self.uri, PROV['generatedAtTime'],
                      Literal(timestamp, datatype=XSD.dateTime)))


    def ingest(self, graph, target_graph=None):
        """
        Adds all triples in the RDFLib ``graph`` to this :class:`Nanopublication` dataset.
        If ``target_graph`` is ``None``, then the triples are added to the default graph,
        otherwise they are added to the indicated graph
        """
        if target_graph is None:
            for s, p, o in graph:
                self.add((s, p, o))
        else:
            for s, p, o in graph:
                self.add((s, p, o, target_graph))
