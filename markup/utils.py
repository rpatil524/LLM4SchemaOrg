from copy import deepcopy
from hashlib import md5
from io import BytesIO
import itertools
import json
import os
from pathlib import Path
from pprint import pprint
import re
from typing import Any, Dict, List, Union, Literal as LiteralType
from urllib.error import URLError
from urllib.parse import quote_plus
import warnings
from SPARQLWrapper import SPARQLWrapper2
import SPARQLWrapper
import backoff
import html2text
from bs4 import BeautifulSoup
import httpx
from llama_cpp import Llama
from openai import OpenAI
from pydantic import BaseModel
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

import trafilatura
from warcio.archiveiterator import ArchiveIterator

from rdflib import RDF, RDFS, BNode, ConjunctiveGraph, Graph, Literal, URIRef
import extruct

# Download NLTK data if you haven't already
import nltk
# nltk.download('punkt')
nltk.download('stopwords')
# nltk.download('wordnet')
# nltk.download('omw-1.4')

from nltk.tokenize import word_tokenize
from nltk.metrics.distance import jaccard_distance
from nltk.corpus import stopwords

import spacy

LLAMA_CPP_CONFIG = "configs/llama_cpp.yaml"

import tiktoken
from chunkipy import TextChunker, TokenEstimator

import json_repair
import coloredlogs, logging

# Configure logging
LOG_LEVEL = logging.DEBUG
logging.basicConfig(level=LOG_LEVEL,format='%(asctime)s - %(levelname)s - %(message)s')

# Create a file handler
file_handler = logging.FileHandler('logfile.log')
file_handler.setLevel(LOG_LEVEL)

# Create a stream handler (for logging to the screen)
# stream_handler = logging.StreamHandler()
# stream_handler.setLevel(logging.INFO)

# Create a formatter and set it for both handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
# stream_handler.setFormatter(formatter)

# Add the handlers to the logger
logger = logging.getLogger()
logger.addHandler(file_handler)
# logger.addHandler(stream_handler)

# Set up colored logging for the screen
coloredlogs.install(level=logging.getLevelName(LOG_LEVEL), fmt='%(asctime)s - %(levelname)s - %(message)s')

CC_INDEX_SERVER = 'http://index.commoncrawl.org/'
LANGUAGES_CACHE_FILE = ".cache/languages.cache"  
INDEX_NAME = 'CC-MAIN-2022-40'

class LlamaCPPError(Exception):
    pass

class BinaryPrediction(BaseModel):
    label: LiteralType["TOKPOS", "TOKNEG"]

def get_infos(prop, value, ent_type):  
    if ent_type is None:
        return ["[TOK_Q_DELIM]".join((prop, str(value), str(ent_type)))]
    if isinstance(ent_type, str):
        ent_type = [ent_type]
    result = []
    for et in ent_type:
        result.append("[TOK_Q_DELIM]".join((prop, str(value), et)))
    return result

def is_json_disjoint(stub, json2: dict, key=None):
    is_disjoint = True
    if isinstance(stub, dict):
        for k, v in stub.items():
            if not is_json_disjoint(v, json2, key=k):
                is_disjoint = False
                break

    elif isinstance(stub, list):
        for v in stub:
            if not is_json_disjoint(v, json2, key=key):
                is_disjoint = False
                break
    else:
        if len(jsonld_search_property(json2, key=key, value=stub)) > 0:
            is_disjoint = False
    return is_disjoint

def preprocess_text(doc: str):
    # Tokenize the text
    tokens = word_tokenize(doc)
    
    # Remove punctuation and stop words
    tokens = [token for token in tokens if token.isalnum() and token not in set(stopwords.words('english'))]
    tokens = list(itertools.chain(*[camel_case_split(token) for token in tokens]))
    tokens = [token.lower() for token in tokens]
    
    return set(tokens)

def jaccard_similarity(doc1: str, doc2: str):
    a = preprocess_text(doc1)
    b = preprocess_text(doc2)
    return 1-jaccard_distance(a, b)

def jaccard_similarity_multiset(multiset1, multiset2):
    # Convert dictionaries to lists of elements
    elements1 = [element for element, count in multiset1.items() for _ in range(count)]
    elements2 = [element for element, count in multiset2.items() for _ in range(count)]
    
    # Calculate Jaccard similarity
    intersection = sum((min(elements1.count(item), elements2.count(item)) for item in set(elements1) & set(elements2)))
    union = sum((max(elements1.count(item), elements2.count(item)) for item in set(elements1) | set(elements2)))

    return intersection / union

def compare_graphs_on_pred(graph1, graph2):
    # Get the properties of a class
    query = """
    PREFIX schema: <https://schema.org/>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>  
    SELECT ?p (count(?o) as ?count) where {
        ?s ?p ?o .
    }  group by ?p order by desc(?count)
    """
    print(query)
    results1 = graph1.query(query)
    json1 = {str(result[0]) : int(result[1]) for result in results1}
    print(f"graph 1: {json1}")

    results2 = graph2.query(query)
    json2 = {str(result[0]) : int(result[1]) for result in results2}
    print(f"graph 2: {json2}")

    union_g1_g2 = {k: max(json1.get(k, 0),json2.get(k, 0)) for k in set(json1) | set(json2)} 
    print(f"union: {union_g1_g2}")  

    jaccard1 = jaccard_similarity_multiset(json1, union_g1_g2)    
    jaccard2 = jaccard_similarity_multiset(json2, union_g1_g2)    
    return jaccard1, jaccard2

def extract_json(document: str):
    # Attempt to fix the JSON object
    decoded_object = json_repair.loads(document)
    return decoded_object

def camel_case_split(s):
    words = [[s[0]]]
 
    for c in s[1:]:
        if words[-1][-1].islower() and c.isupper():
            words.append(list(c))
        else:
            words[-1].append(c)
 
    result = [''.join(word) for word in words]
    print(result)
    return result

def embed(word):
    nlp = spacy.load("en_core_web_md")
    return nlp(" ".join(camel_case_split(word)))
    
def html_to_rdf_extruct(html_source) -> ConjunctiveGraph:
        id = Path(html_source).stem
    
        data = None
        
        with open(html_source, "r") as f:
            # null byte issue: https://github.com/scrapinghub/extruct/issues/112
            html_content = f.read().strip().replace('\x00', '').encode('utf8')  
            data = extruct.extract(
                html_content, syntaxes=["microdata", "rdfa", "json-ld"], errors="ignore"
                # html_content, syntaxes=["json-ld"], errors="ignore"
            )
        
        kg_jsonld = ConjunctiveGraph()
        if "json-ld" in data.keys():
            for md in data["json-ld"]:
                kg_jsonld += kg_jsonld.parse(
                    data=json.dumps(md, ensure_ascii=False),
                    format="json-ld",
                    publicID=id,
                )

        kg_rdfa = ConjunctiveGraph()
        if "rdfa" in data.keys():
            for md in data["rdfa"]:
                kg_rdfa += kg_rdfa.parse(
                    data=md,
                    format="application/rdf+xml",
                    publicID=id,
                )

        kg_microdata = ConjunctiveGraph()
        if "microdata" in data.keys():
            for md in data["microdata"]:
                kg_microdata += kg_microdata.parse(
                    data=md,
                    format="application/rdf+xml",
                    publicID=id,
                )

        kg_extruct = kg_jsonld + kg_rdfa + kg_microdata

        return kg_extruct
    
def jsonld_search_property(stub, key, value=None, parent=False, keep_parent_class=False, exit_on_first=False): 
    """Recursively search for a property and value (optional) in a JSONLD

    Args:
        stub (_type_): _description_
        key (_type_): _description_
        parent (boolean): returns the parent entity if True, or just the property-value pair if False

    Returns:
        _type_: a list of stubs that correspond to search criteria
    """

    results = []

    def equals(a, b):
        if isinstance(a, list) and isinstance(b, list):
            return sorted(a) == sorted(b)
        return a == b

    if isinstance(stub, dict):

        for k, v in stub.items():
            if (
                (value is not None and v is not None and k == key and equals(v, value)) or
                (value is None and k == key)
            ):
                parent_class = stub.get("@type")
                search_result = stub if parent else { key: stub[key] }

                if keep_parent_class and parent_class:
                    search_result["@type"] = parent_class
                                
                results.append(search_result)
                if exit_on_first: 
                    return results
            
            result = jsonld_search_property(v, key, value=value, parent=parent, keep_parent_class=keep_parent_class)
            if result: 
                results.extend(result)
                if exit_on_first:
                    return results
            
    elif isinstance(stub, list):
        for item in stub:
            result = jsonld_search_property(item, key, value=value, parent=parent, keep_parent_class=keep_parent_class)
            if result: 
                results.extend(result)
                if exit_on_first:
                    return results
    # raise ValueError(f"Could not find {key} in {stub}")
    return results
        
def get_schema_example(schema_url, include_ref=False, focus=False):
        
    """Scrape the schema.org page for examples

    Args:
        schema_url (_type_): schema.org page for property or class
        focus (bool, optional): only retrieve the part where `schema_url` is a item key in jsonld example. Defaults to False.

    Returns:
        _type_: _description_
    """
    
    results = []       
    
    query = f"""
    SELECT ?ref ?jsonld WHERE {{
        {URIRef(schema_url).n3()} <http://example.org/hasExample> ?example .
        ?example    <http://example.org/json> ?jsonld ;
                    <http://example.org/pre-markup> ?ref .
    }}
    """
    
    examples = []

    # TODO: Preferrably to load the turtle into a virtuoso and send queries there
    schemaorg_examples_endpoint = "http://localhost:5002/" 
    qres = None
    try: 
        qres = sparql_query(schemaorg_examples_endpoint, query)
    except URLError:
        raise URLError("Could not connect to the SPARQL endpoint! Make sure you ran 'rdflib-endpoint serve --port 5002 schemaorg/examples/schemaorg-all-examples.ttl'")
    
    for qr in qres:
        ref = qr.get("ref").value
        jsonld = qr.get("jsonld").value
        examples.append((ref, jsonld))
        
    for ref, example in examples:
        # jsonld_str = example.find("pre", class_="prettyprint").get_text().strip().split("\n")
        soup = BeautifulSoup(example, "html.parser")
        q = soup.find("script")
        jsonld_str = q.get_text() if q else soup.get_text()

        try: jsonld = json.loads(jsonld_str)
        except json.decoder.JSONDecodeError:
            logger.warning(f"Example is not a valid json! {jsonld_str}")
            continue

        if focus:
            jsonlds = jsonld_search_property(jsonld, schema_simplify(URIRef(schema_url)))
            for jsonld in jsonlds:
                results.append((ref, json.dumps(jsonld, ensure_ascii=False)))
        else:
            results.append((ref, json.dumps(jsonld, ensure_ascii=False)))
    
    if not include_ref:
        return [ ex for _, ex in results ]
    return results
    
def schema_stringify(node):
    if isinstance(node, dict):
        return json.dumps(node, ensure_ascii=False)
    elif isinstance(node, list):
        return ", ".join([ schema_stringify(n) for n in node])
    else:
        return schema_simplify(node)

def schema_simplify(node):

    def clean_uri(result):
        if result.startswith("http"):
            return result.replace("http://schema.org/", "")
        elif result.startswith("file://"):
            return Path(result).name
        return result

    if isinstance(node, URIRef):
        result = node.n3().strip("<>")
        return clean_uri(result)
    elif isinstance(node, Literal):
        return node.value or str(node)
    elif isinstance(node, list):
        return [ schema_simplify(n) for n in node ]
    elif isinstance(node, (float, int)): # Python primitives
        return node
    elif isinstance(node, dict):
        result = {}
        for k,v in node.items():
            result[schema_simplify(k)] = schema_simplify(v)
        return result
    elif isinstance(node, SPARQLWrapper.SmartWrapper.Value):
        return clean_uri(node.value)
    elif isinstance(node, str):
        return clean_uri(node)
    else:
        raise NotImplementedError(f"{type(node)} of value {node} is not yet supported!")

def extract_preds(graph: ConjunctiveGraph, ref_type, root=None, visited: list=[], depth_limit=1, depth=0, simplify=False):
    results = set()
    if root is None:
        for s in graph.subjects(URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(ref_type)):
            visited.append(s)
            results.update(
                extract_preds(
                    graph, ref_type, root=s, simplify=simplify,
                    visited=visited, depth_limit=depth_limit, depth=depth
                )
            )
    else:
        for p, o in graph.predicate_objects(root):
            results.add(p)
            if o not in visited and depth < depth_limit:
                visited.append(o)
                results.update(
                    extract_preds(
                        graph, ref_type, root=o, simplify=simplify,
                        visited=visited, depth_limit=depth_limit, depth=depth+1
                    )
                )
                
    if simplify:
        results = set([ schema_simplify(p) for p in results ])
                
    return results

def jsonld_augmented(jsonld, level=0):
    stub = deepcopy(jsonld)
    if isinstance(jsonld, dict):
        for k, v in jsonld.items():
            logger.debug(f"Augmenting key={k}, value={v}...")
            if k == "@type":
                if isinstance(v, str):
                    v = [v]
                stub[k] = v
                for i, val in enumerate(v):
                    if not val.startswith("http://schema.org/"):
                        stub[k][i] = f"http://schema.org/{val}"
            elif k in ["@id", "@context"]:
                stub[k] = v
            else:
                stub.pop(k)
                if not k.startswith("http://schema.org/"):
                    k = f"http://schema.org/{k}"
                stub[k] = jsonld_augmented(v, level=level+1)
        if level == 0:
            stub["@context"] = "http://schema.org"
    elif isinstance(jsonld, list):
        for i, v in enumerate(jsonld):
            logger.debug(f"Augmenting value={v}...")
            stub[i] = jsonld_augmented(v, level=level+1)

    return stub
    
def to_jsonld(rdf, simplify=False, clean=False, keep_root=False, attempt_fix=False):
    
    g = None
    if isinstance(rdf, Graph):
        g = rdf
    elif rdf.endswith(".json") or rdf.endswith(".jsonld"):
        with open(rdf, "r") as f:
            jsonld = json.load(f)
            # if len(jsonld_search_property(jsonld, key="@context", exit_on_first=True)) == 0:
            #     jsonld = jsonld_augmented(jsonld)
            #     logger.info(f"Parsing JSON-LD from {rdf} ...")
            #     g = ConjunctiveGraph()
            #     g.parse(data=json.dumps(jsonld), format="json-ld")
            #     return jsonld
            # else:
            if simplify:
                return jsonld
            else:   
                logger.info("Augmenting JSON-LD...")
                augmented = jsonld_augmented(jsonld)
                logger.info("Done!")
            return augmented
    else:
        g = ConjunctiveGraph()
        g.parse(rdf)
    
    # Suppose that the input jsonld is filtered by type
    # if filter_by_type:
    #     subgraph = ConjunctiveGraph()
    #     for target_class in filter_by_type:
    #         subgraph += filter_graph(g, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(target_class))
        
    #     g = subgraph
         
    # Build a basic dictionary with RDFlib objects       
    bnode_info = {}
    for s, p, o in g:
        if str(p) == "" or str(p) == "":
            continue
        if isinstance(s, (BNode, URIRef)):
            if s not in bnode_info.keys():
                bnode_info[s] = {}
            
            if p == URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"):
                if bnode_info[s].get("@type") is None:
                    bnode_info[s]["@type"] = []
                
                if attempt_fix:
                    o_canonical = lookup_schema_type(o, default=o)
                    bnode_info[s]["@type"].append(o_canonical)
                else:
                    bnode_info[s]["@type"].append(o)
                continue
            
            if p not in bnode_info[s].keys():
                bnode_info[s][p] = []
            bnode_info[s][p].append(o)
        if isinstance(o, (BNode, URIRef)):
            if o not in bnode_info.keys():
                bnode_info[o] = {}  
                
    # Replace BNodes with their real values
    def __replace_bnodes(D: dict, entity_value, r: set, entity_key=None):

        result = entity_value
        if isinstance(entity_value, dict):
            result = {}
            clone = deepcopy(entity_value)
            while len(clone) > 0:
                k, v = clone.popitem()  
                                               
                if k in [URIRef("http://schema.org/url"), "@type"]: 
                    result[k] = v
                    continue
                
                # When it is a empty dictionary, mark for remove
                if isinstance(v, dict) and len(v) == 0:
                    r.add(k)
                else:
                    stub, r = __replace_bnodes(D, v, r, entity_key=k)
                    result[k] = stub
                        
        elif isinstance(entity_value, list):
            result = [ __replace_bnodes(D, item, r, entity_key=entity_key)[0] for item in entity_value ]
            
        elif isinstance(entity_value, (BNode, URIRef)):
            # Key Error means that the type assertion triple was dropped silently by rdflib, 
            # indicating type error that should be picked up by shacl
            try:
                result = D.get(entity_value)
                # If item is an bnode with URI as id...
                if isinstance(entity_value, URIRef): 
                    if result is not None:       
                        # ... if the item type is not expected, then it's a simple URIRef
                        expected_types = get_expected_types(entity_key, simplify=False)
                        if expected_types is not None and result.get("@type") not in expected_types:
                            # logger.warning(f"{result['@type']} not in {expected_types}")
                            result = entity_value.toPython()  
                        # ... item is a bnode has information
                        elif isinstance(result, dict) and len(result) > 0:
                            result["@id"] = entity_value.toPython()
                        # .. item is a simple URIRef
                        else:
                            result = entity_value.toPython()   
                    # ... item is a simple URIRef, nothing to do
                    else:
                        result = entity_value.toPython()    
                
                r.add(entity_value)
                result, r = __replace_bnodes(D, result, r, entity_key=entity_key)
                
            except KeyError as err:
                warnings.warn(f"{err}. It means that the related type assertion triple was dropped silently by rdflib, hinting a type error that could be picked up by SHACL. Assinging None...")
        
        elif isinstance(entity_value, Literal):
            result = entity_value.toPython() 
            
        return result, r

    # Remove redundant BNodes
    bnode_info, redundants = __replace_bnodes(bnode_info, bnode_info, set())
    for redundant in redundants:
        if redundant in bnode_info.keys():
            bnode_info.pop(redundant)

    # Remove root BNodes with the actual markup
    if len(bnode_info) == 1 and not keep_root:
        bnode_info = list(bnode_info.values())[0]
        bnode_info["@context"] = "http://schema.org"
    else:
        for k in bnode_info.keys():
            bnode_info[k]["@context"] = "http://schema.org"
                        
    # Simplify
    if simplify:        
        bnode_info = transform_json(bnode_info, schema_simplify, schema_simplify)
    
    # Clean
    if clean:
        bnode_info = clean_json(bnode_info)
        
    return bnode_info

def clean_json(stub):
    def do_clean(v):
        if isinstance(v, list):
            return v[0] if len(v) == 1 else v
        return v
    result = transform_json(
        stub,
        value_transformer=do_clean
    )
    return result

def filter_json(stub, key, value=None, parent_class=None):
    clone = deepcopy(stub)
    if isinstance(clone, dict):
        for k in stub.keys():
            v = clone[k]
            new_v = filter_json(v, key, value=value, parent_class=parent_class)
            # Delete if value is BNode ID or value down the line
            can_delete_v = k == key and (new_v is None or re.search(r"^[0-9a-zA-Z]{30,}$", value) is not None)
                
            # Skip until parent_class is met
            stub_type = stub.get("@type")
            logger.debug(f"Stub type: {stub_type}, parent={parent_class}")

            if stub_type and parent_class:
                is_type_shared = False
                if isinstance(stub_type, str):
                    is_type_shared = stub_type == parent_class
                elif isinstance(stub_type, list):
                    is_type_shared = len(set(stub_type).intersection(set([parent_class]))) > 0
                else:
                    raise ValueError(f"Type {type(stub_type)} is not supported!")
                
                if not is_type_shared:
                    logger.debug(f"Assigning {new_v} to {k} then skip...")
                    clone[k] = new_v
                    continue

            logger.debug(f"k={k}, v={v}, query_k={key}, query_v={value}, new_v={new_v}")

            # If filtering by type: delete the entire entity
            if key == "@type" and can_delete_v:
                logger.debug(f"Found @type")
                return None
            # If filtering by key-value: delete the corresponding key
            elif key != "@type" and can_delete_v:
                logger.debug(f"Removing {k}")
                clone.pop(k)
                logger.debug(f"{clone}")
            else:            
                logger.debug(f"Assigning {new_v} to {k}")
                clone[k] = new_v
    elif isinstance(clone, list):
        tmp = []
        for item in clone:
            item = filter_json(item, key, value=value, parent_class=parent_class)
            if item is not None: 
                tmp.append(item)
        
        # Delete empty lists
        clone = tmp if len(tmp) > 0 else None
    else:
        if value is not None: 
            clone = stub
            if str(stub) == str(value):
                return None
    return clone

def transform_json(stub, key_transformer=None, value_transformer=None):  
    key_transformer = key_transformer or (lambda k: k)
    value_transformer = value_transformer or (lambda v: v)
    
    if isinstance(stub, list):
        result = [ transform_json(item, key_transformer, value_transformer) for item in stub ]    
        return result[0] if len(result) == 1 else result
    elif isinstance(stub, dict):
        # visited = set()
        result = {}
        ent_type = None
        for k, values in stub.items():
            if k == "@type":
                ent_type = value_transformer(values)
                result["@type"] = ent_type
                continue
            
            if k in ["@context", "@id"]: 
                result[k] = values
                continue

            new_k = key_transformer(k)
            
            # Recursively add prompt for dependant entities             
            if values is None: continue
            result[new_k] = transform_json(values, key_transformer, value_transformer)
            # visited.add(k)
        
        return result
    else:
        return value_transformer(stub)

def collect_json(stub, *args, key_filter=lambda k,e: True, value_transformer=lambda k,v,e: v, **kwargs) -> List[Any]:
    """_summary_

    Args:
        stub (_type_): _description_
        e (True): _description_
        v (_type_): _description_
        e (v): _description_
        key_filter (_type_, optional): _description_. Defaults to lambdak.
        value_transformer (_type_, optional): _description_. Defaults to lambdak.

    Returns:
        List[Any]: _description_
    """
    results = []
    if isinstance(stub, dict):
        ent_type = stub.get("@type")
        for k, values in stub.items():

            if k == "@type":
                continue
            
            if k in ["@context", "@id"]: continue

            # Recursively add prompt for dependant entities             
            if values is None: continue
            args = [k, values, ent_type]
            
            if key_filter(k, ent_type):
                results.extend(collect_json(values, *args, key_filter=key_filter, value_transformer=value_transformer, **kwargs))
                                
    elif isinstance(stub, list):
        for item in stub:
            if len(args) > 0:
                k, _, e = args
                args = [k, item, e]
            # logger.debug(f"list item: level {level} {len(item)}")
            results.extend(collect_json(item, *args, key_filter=key_filter, value_transformer=value_transformer, **kwargs))
    else:
        new_v = value_transformer(*args, **kwargs)
        results.append(new_v)
    return results

def sparql_query(endpoint, query):
    sparql = SPARQLWrapper2(endpoint)
    sparql.setQuery(query)
    #sparql.setReturnFormat("json")
    results = sparql.query().bindings
    return results

def get_type_definition(class_=None, prop=None, parents=True, simplify=False, 
                        include_expected_types=False, include_comment=False,
                        exit_on_first=False) -> Union[Dict, List]:
    """Get the definition for specific Schema.org class. 
    The result is a list of predicate or a dictionary with predicate as key, 
    expected types and comment as values.

    Args:
        schema_type_url (_type_, optional): the url to schema.org class
        prop (_type_, optional): the url to schema.org property
        parents (bool, optional): whether or not retrieve properties of parent classes (for class only)
        simplify (bool, optional): whether or not simplify the URI, e.g, http://schema.org/Painting -> Painting
        include_expected_types (bool, optional): whether or not include information regarding the expected types
        include_expected_comment (bool, optional): whether or not include information regarding the comment

    Returns:
        List[Any]: a list of definitions
    """
    results = dict()    
    prop_var = "?prop"
    if prop:
        try:
            prop_var = URIRef(prop).n3()
        except Exception as e:
            logger.error(f"Could not parse {prop} as URIRef: {e}")
            return []
            
    domain_var = URIRef(class_).n3() if class_ and prop is None else "?domain"
    
    # Get the attribute of class
    query = f"""
    SELECT ?prop ?range ?comment WHERE {{
        {prop_var} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property> .
        {prop_var} <http://schema.org/domainIncludes> {domain_var} .
        {prop_var} <http://schema.org/rangeIncludes> ?range .
        {prop_var} <http://www.w3.org/2000/01/rdf-schema#comment> ?comment .
    }}
    """

    if exit_on_first:
        query += " LIMIT 1"
    
    # TODO: Preferrably to load the turtle into a virtuoso and send queries there
    
    schemaorg_type_def_endpoint = ("http://localhost:5001/")    
    
    qresults = None
    try:
        qresults = sparql_query(schemaorg_type_def_endpoint, query)    
    except URLError:
        raise URLError("Could not connect to the SPARQL endpoint! Make sure you ran 'rdflib-endpoint serve --port 5001 schemaorg/schemaorg-all-http.nt'")
    
    for row in qresults:
        prop_clean = row.get("prop")
        if prop is not None:
            prop_clean = URIRef(prop) 
        expected_type = row.get("range").value
        comment = row.get("comment").value.strip()
        if simplify:
            prop_clean = schema_simplify(prop_clean)
            expected_type = schema_simplify(expected_type)
        # else:
        #     if prop is None:
        #         prop_clean = prop_clean.n3()
        #     expected_type = expected_type.n3()

        if prop_clean not in results:
            results[prop_clean] = dict()

        if include_expected_types:
            if results[prop_clean].get("expected_types") is None:
                results[prop_clean]["expected_types"] = []
            
            if expected_type not in results[prop_clean]["expected_types"]:
                results[prop_clean]["expected_types"].append(expected_type)
        
        if include_comment:
            results[prop_clean]["comment"] = comment
    
    if not include_expected_types and not include_comment:
        results = list(results.keys())
            
    # Recursively get the attributes of parent classes
    if parents and class_:
        tmp = sparql_query(schemaorg_type_def_endpoint, f"""SELECT ?parent WHERE {{ {URIRef(class_).n3()} <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?parent }}""")
        parent_classes = [ row.get("parent").value for row in tmp ]
        for parent_class in parent_classes:
            p_results = get_type_definition(parent_class, prop=prop, simplify=simplify, include_expected_types=include_expected_types, include_comment=include_comment)
            
            if include_expected_types or include_comment:
                results.update(p_results)
            else:
                results.extend(p_results)

    return results

def get_expected_types(prop, **kwargs):
    """Get the expected type of a schema.org property

    Args:
        prop (_type_): a canonical schema.org property url
        kwargs: the kwargs for get_type_definition

    Returns:
        _type_: a list of expected types
    """
    prop_name = schema_simplify(URIRef(prop)) if kwargs.get("simplify") == True else prop
    definition = get_type_definition(prop=prop, **kwargs, include_expected_types=True)
    
    if len(definition) == 0: 
        return None
    
    results = definition.get(prop_name)["expected_types"]
    if kwargs.get("simplify") == True:
        logger.debug(f"Expected types for {prop_name}: {results}")
        results = [ schema_simplify(result) for result in results ]
                
    return results

def md5hex(obj):
    return md5(str(obj).encode()).hexdigest()

@backoff.on_predicate(backoff.expo, predicate=lambda x: x['status_code'] not in [200, 404, 503])
@backoff.on_exception(backoff.expo, (urllib3.exceptions.MaxRetryError, requests.exceptions.ProxyError, requests.exceptions.RetryError))
def search_cc_index(url):  
    encoded_url = quote_plus(url)
    index_url = f'{CC_INDEX_SERVER}{INDEX_NAME}-index?url={encoded_url}&output=json'
        
    response = requests.get(index_url)
    # logger.debug("Response from CCI:", response.text)  # Output the response from the server
    
    data = None
    status_code = response.status_code
    
    if response.status_code == 200:
        reponse_text = response.text.strip()
        if len(reponse_text) == 0:
            raise RuntimeError("CommonCrawl returned empty response with code 2OO!")
        records = reponse_text.split('\n')
        data = [json.loads(record) for record in records]
    
    result = { "status_code": status_code, "data": data }
    logger.debug(f"CommonCrawl index search result: {result}")
    return result
    
        
def lang_detect(target_url):
    languages = None
    Path(LANGUAGES_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
    LANGUAGES_CACHE = dict()
    if os.path.exists(LANGUAGES_CACHE_FILE):
        with open(LANGUAGES_CACHE_FILE, "r") as cache_file:
            LANGUAGES_CACHE = json.load(cache_file)

    md5ingest = md5hex(target_url)
    if md5ingest in LANGUAGES_CACHE:
        languages = LANGUAGES_CACHE[md5ingest]
    else:   
        records = search_cc_index(target_url)["data"]
        if records:
            for record in records:
                languages = record["languages"].split(",") if "languages" in record else ["unknown"]
                LANGUAGES_CACHE[md5ingest] = languages

            with open(LANGUAGES_CACHE_FILE, "w") as cache_file:
                json.dump(LANGUAGES_CACHE, cache_file, ensure_ascii=False) 
    return languages

def get_page_content(target_url):

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    http = requests.Session()
    http.mount("https://", adapter)
    http.mount("http://", adapter)

    # Please note: f-strings require Python 3.6+

    # The URL of the Common Crawl Index server
    Path(LANGUAGES_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
    LANGUAGES_CACHE = dict()
    if os.path.exists(LANGUAGES_CACHE_FILE):
        with open(LANGUAGES_CACHE_FILE, "r") as cache_file:
            LANGUAGES_CACHE = json.load(cache_file)

    md5ingest = md5hex(target_url)

    # Function to fetch the content from Common Crawl
    def fetch_page_from_cc(records):
        for record in records:
            offset, length = int(record['offset']), int(record['length'])
            prefix = record['filename'].split('/')[0]
            base_name = os.path.basename(record['filename'])
            languages = record["languages"].split(",") if "languages" in record else ["unknown"]
            LANGUAGES_CACHE[md5ingest] = languages

            with open(LANGUAGES_CACHE_FILE, "w") as cache_file:
                json.dump(LANGUAGES_CACHE, cache_file, ensure_ascii=False)
            dest_url = record["url"]

            # Filter page with English as the only language
            # if not (len(languages) == 1 and "eng" in languages):
            #     raise RuntimeError("Skipping because the content is not in English!")

            s3_url = f'https://data.commoncrawl.org/{record["filename"]}'
            headers = {'Range': f'bytes={offset}-{offset+length-1}'}
            response = http.get(s3_url, headers=headers)
            if response.status_code == 206:
                # Process the response content if necessary
                # For example, you can use warcio to parse the WARC record
                
                with BytesIO(response.content) as stream:
                    for r in ArchiveIterator(stream):
                        # Check if the current record is a response record and has the expected URL
                        if r.rec_type == 'response' and r.rec_headers.get_header('WARC-Target-URI') == dest_url:
                            # Extract the content of the response
                            page_content = r.content_stream().read().decode('utf-8')
                            return page_content
            else:
                raise ConnectionError(f"Failed to fetch data: status {response.status_code}, message: {response.content.decode()}")
        
    cache_file = f".cache/{md5ingest}_raw.html"
    if not os.path.exists(cache_file):        
        # Search the index for the target URL
        records = search_cc_index(target_url)["data"]
        logger.debug(f"Found {len(records)} records for {target_url}")
        if records:
            logger.debug(f"Found {len(records)} records for {target_url}")

            # Fetch the page content from the first record
            content = fetch_page_from_cc(records)
            if content:
                logger.info(f"Successfully fetched content for {target_url}")
                # You can now process the 'content' variable as needed
                with open(cache_file, "w") as f:
                    f.write(content)
        else:
            logger.debug(f"No records found for {target_url}")
            return None
    return scrape_webpage(cache_file)

def _html2txt(content, force=False):
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.ignore_tables = False
    converter.ignore_emphasis = False
                                 
    return converter.handle(content)

def _trafilatura(content, accept_threshold=0.3, verbose=False):
    """Extract content from a webpage. If the output contains less than 30% of the input tokens, reject (@cite CCNet: 70% of the page content is boilerplate).
    """
    html_ref = BeautifulSoup(content, "html.parser").get_text()
    html_clean = trafilatura.extract(content, include_links=True, include_images=True, include_tables=True, deduplicate=True)

    tok_ref = len(re.split(r"\s+", html_ref)) if html_ref else None
    tok_clean = len(re.split(r"\s+", html_clean)) if html_clean else None

    clean_ratio = tok_clean / tok_ref if tok_ref is not None and tok_clean is not None else None
    if clean_ratio is not None and clean_ratio > accept_threshold:
        return html_clean if not verbose else (html_clean, tok_ref, tok_clean, clean_ratio)
    
    print(f"Could not clean webpage (clean_ratio: {tok_clean}/{tok_ref} = {clean_ratio})")
    return None if not verbose else (None, tok_ref, tok_clean, clean_ratio)

def scrape_webpage(cache_file):
    
    with open(cache_file, "r") as f:
        content = f.read()
        try:
            text = _html2txt(content)
            # text = _trafilatura(content)
            return text
        except RuntimeError as e:
            raise RuntimeError(f"{str(e)}. HTML file: {cache_file}")
       
def filter_graph(graph: ConjunctiveGraph, subj=None, pred=None, obj=None, root=None, visited: list=[]):
    """Extract a subgraph where the root is an entity with certain type

    Args:
        graph (ConjunctiveGraph): _description_
        schema_type (_type_): _description_
        root (_type_, optional): _description_. Defaults to None.

    Returns:
        _type_: _description_
    """
    result = ConjunctiveGraph()
        
    if root is None:
        visited = []
        for s, _, _ in graph.triples((subj, pred, obj)):
            subgraph = filter_graph(graph, subj=subj, pred=pred, obj=obj, root=s, visited=visited)
            for s1, p, o in subgraph:
                result.add((s1, p, o))
                visited.append(s)
    else:
        for p, o in graph.predicate_objects(root):
            result.add((root, p, o))
                        
            if o not in visited:      
                visited.append(o)
                subgraph = filter_graph(graph, obj, root=o, visited=visited)
                for s1, p1, o1 in subgraph:
                    result.add((s1, p1, o1))
    return result
        
def lookup_schema_type(schema_type, default=None, verbose=False):
    """Lookup the canonical form for a schema.org type. For example, localbusiness -> LocalBusiness
    """
    
    query = f"""
    SELECT ?class WHERE {{
        ?class <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2000/01/rdf-schema#Class> .
        FILTER ( contains( lcase(str(?class)), {repr(str(schema_type).lower())}) )
    }}
    """
    
    schemaorg_type_def_endpoint = ("http://localhost:5001/")    
    
    qresults = None
    try:
        qresults = sparql_query(schemaorg_type_def_endpoint, query)    
    except URLError:
        raise URLError("Could not connect to the SPARQL endpoint! Make sure you ran 'rdflib-endpoint serve --port 5001 schemaorg/schemaorg-all-http.nt'")

    candidates = []
    for row in qresults:
        candidate = row.get("class")
        candidate_simple = schema_simplify(candidate)
                
        ref_simple = schema_simplify(schema_type)
        
        jaccard_index = len(set(candidate_simple) & set(ref_simple)) / len(set(candidate_simple) | set(ref_simple))
        candidates.append((candidate, jaccard_index))
        
    if len(candidates) == 0:
        return default
        
    best_candidate, best_score = max(candidates, key=lambda x: x[1])
    logger.debug(f"Best candidate for {schema_type} is {best_candidate}, jaccard={best_score}")
    return best_candidate if not verbose else candidates

def ping(url):
    try:
        response = requests.get(url, allow_redirects=False)
        return (200 <= response.status_code < 300)
    except:
        return False

class TiktokenEstimator(TokenEstimator):
    def __init__(self) -> None:
        self._encoding = tiktoken.get_encoding("cl100k_base")

    def estimate_tokens(self, text):
        return len(self._encoding.encode(text)) 

class LlamaCPPEstimator(TokenEstimator):
    def __init__(self, llama_model) -> None:
        self._llm = llama_model
    
    def estimate_tokens(self, text: str):
        if isinstance(self._llm, Llama):
            return len(self._llm.tokenize(text.encode("utf-8")))
        elif isinstance(self._llm, OpenAI):
            # Send a request to the OpenAI API
            response = requests.post(
                url=str(self._llm.base_url).replace("/v1/","/extras/tokenize/count/"),
                headers=self._llm.auth_headers,
                json={"input": text}
            ).json()

            count = response["count"]
            return count
        
        else:
            raise NotImplementedError(f"{type(self._llm)} is not yet supported!")

def chunk_document(document, max_chunk_size, token_estimator: TokenEstimator, overlap_percentage=0.1, verbose=True):
    
    logger.debug(f"Size before chunking: {token_estimator.estimate_tokens(document)} tokens!")

    text_chunker = TextChunker(max_chunk_size, tokens=True, token_estimator=token_estimator, split_strategies=[word_tokenize], overlap_percent=overlap_percentage)
    chunks = text_chunker.chunk(document)
    return chunks

