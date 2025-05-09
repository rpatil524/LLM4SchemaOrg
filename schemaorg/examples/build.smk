from itertools import chain
import json
import os
import pandas as pd
from tqdm import tqdm

tqdm.pandas()


import sys
sys.path.append(os.path.join(os.getcwd(), "markup"))

from utils import get_infos, collect_json, get_type_definition

SCHEMAORG_DIR = "schemaorg/examples"

def extract_items(data):
    data = json.loads(data) 
    return list(set(chain(*collect_json(data, value_transformer=get_infos))))

def split_items(row):
    item = row["example_snippet"]
    row["prop"], row["value"], row["type"] = item.split("[TOK_Q_DELIM]")
    return row        

rule all:
    input:
        factual_extrinsic_json = f"{SCHEMAORG_DIR}/factual-extrinsic.json",
        factual_intrinsic_json = f"{SCHEMAORG_DIR}/factual-intrinsic.json",
        semantic_json = f"{SCHEMAORG_DIR}/semantic.json"

rule clean_factual_extrinsic:
    input: f"{SCHEMAORG_DIR}/factual-extrinsic.parquet",
    output: f"{SCHEMAORG_DIR}/factual-extrinsic.json",
    run:
        parquet = pd.read_parquet("schemaorg/examples/factual-extrinsic.parquet")
        parquet["example_snippet"] = parquet["example_snippet"].apply(extract_items)
        parquet = parquet.explode("example_snippet")
        parquet = parquet.apply(split_items, axis=1)
        parquet.drop(columns=["example_snippet", "example"], inplace=True)
        parquet = parquet.reindex(columns=["ref", "prop", "value", "type", "label"])

        print(parquet["label"].value_counts())

        cleaned = parquet.to_dict(orient="records")
        with open("schemaorg/examples/factual-extrinsic.json", "w") as f:
            json.dump(list(cleaned), f, ensure_ascii=False, indent=2)

rule clean_factual_intrinsic:
    input: f"{SCHEMAORG_DIR}/factual-intrinsic.parquet",
    output: f"{SCHEMAORG_DIR}/factual-intrinsic.json",
    run:
        parquet = pd.read_parquet(f"{input}")
        parquet["example_snippet"] = parquet["example_snippet"].apply(extract_items)
        parquet = parquet.explode("example_snippet")
        parquet = parquet.apply(split_items, axis=1)
        parquet.drop(columns=["example_snippet", "example"], inplace=True)
        parquet = parquet.reindex(columns=["ref", "prop", "value", "type", "label"])

        print(parquet["label"].value_counts())

        cleaned = parquet.to_dict(orient="records")
        with open(f"{output}", "w") as f:
            json.dump(list(cleaned), f, ensure_ascii=False, indent=2)

rule clean_semantic:
    input: f"{SCHEMAORG_DIR}/semantic.parquet",
    output: f"{SCHEMAORG_DIR}/semantic.json",
    run: 
        def extract_definition(row):
            canon_prop = f"http://schema.org/{row['prop']}"
            canon_type = f"http://schema.org/{row['type']}"
            definition = get_type_definition(class_=canon_type, prop=canon_prop, include_comment=True, simplify=True)
            row["definition"] = definition.popitem()[1]["comment"]
            return row

        parquet = pd.read_parquet(f"{input}")
        parquet["example_snippet"] = parquet["example_snippet"].progress_apply(extract_items)
        parquet = parquet.explode("example_snippet")
        parquet = parquet.progress_apply(split_items, axis=1)
        parquet = parquet.progress_apply(extract_definition, axis=1)
        parquet.drop(columns=["example_snippet", "example"], inplace=True)
        parquet = parquet.reindex(columns=["prop", "definition", "value", "type", "label"])

        print(parquet)

        cleaned = parquet.to_dict(orient="records")
        with open(f"{output}", "w") as f:
            json.dump(list(cleaned), f, ensure_ascii=False, indent=2)

rule merge_factual_extrinsic:
    input: 
        positive=f"{SCHEMAORG_DIR}/compliance-pos.parquet",
        negative=f"{SCHEMAORG_DIR}/factual-extrinsic-neg.parquet"
    output:f"{SCHEMAORG_DIR}/factual-extrinsic.parquet"
    run: 
        pos_df = pd.read_parquet(f"{input.positive}")
        pos_df = pos_df[["ref", "prop", "example", "example_snippet"]]
        pos_df["label"] = "positive"

        fac_neg_s_df = pd.read_parquet(f"{input.negative}")
        fac_neg_s_df["label"] = "negative"

        fac_extrinsic_test_df = pd.concat([pos_df, fac_neg_s_df], ignore_index=True)
        fac_extrinsic_test_df = fac_extrinsic_test_df.iloc[fac_extrinsic_test_df.astype(str).drop_duplicates().index, :]
        fac_extrinsic_test_df.to_parquet(f"{output}")

rule merge_factual_intrinsic:
    input: 
        positive=f"{SCHEMAORG_DIR}/compliance-pos.parquet",
        negative=f"{SCHEMAORG_DIR}/factual-intrinsic-neg.parquet"
    output:"factual-intrinsic.parquet"
    run: 
        pos_df = pd.read_parquet(f"{input.positive}")
        pos_df = pos_df[["ref", "prop", "example", "example_snippet"]]
        pos_df["label"] = "positive"

        fac_neg_c_df = pd.read_parquet(f"{input.negative}")
        fac_neg_c_df["label"] = "negative"

        fac_intrinsic_test_df = pd.concat([pos_df, fac_neg_c_df], ignore_index=True)
        fac_intrinsic_test_df = fac_intrinsic_test_df.iloc[fac_intrinsic_test_df.astype(str).drop_duplicates().index, :]
        fac_intrinsic_test_df.to_parquet(f"{output}")

rule merge_semantic:
    input: 
        positive=f"{SCHEMAORG_DIR}/compliance-pos.parquet",
        negative=ancient(f"{SCHEMAORG_DIR}/compliance-neg.parquet")
    output: f"{SCHEMAORG_DIR}/semantic.parquet"
    run: 
        pos_df = pd.read_parquet(f"{input.positive}")
        pos_df = pos_df[["ref", "prop", "example", "example_snippet"]]
        pos_df["label"] = "positive"

        comp_neg_df = pd.read_parquet(f"{input.negative}")
        comp_neg_df["label"] = "negative"

        comp_test_df = pd.concat([pos_df, comp_neg_df], ignore_index=True)
        comp_test_df = comp_test_df.iloc[comp_test_df.astype(str).drop_duplicates().index, :]
        comp_test_df.to_parquet(f"{output}")

rule build_factual_extrinsic_neg:
    input: ancient(f"{SCHEMAORG_DIR}/compliance-pos.parquet")
    output: f"{SCHEMAORG_DIR}/factual-extrinsic-neg.parquet"
    shell: "python markup/schemaorg_examples_dataset.py generate-negative-examples-halu-extrinsic {input} {output}"

rule build_factual_intrinsic_neg:
    input: ancient(f"{SCHEMAORG_DIR}/compliance-pos.parquet")
    output: f"{SCHEMAORG_DIR}/factual-intrinsic-neg.parquet"
    shell: "python markup/schemaorg_examples_dataset.py generate-negative-examples {input} {output}"

rule build_semantic_neg:
    input: ancient(f"{SCHEMAORG_DIR}/compliance-pos.parquet")
    output: f"{SCHEMAORG_DIR}/compliance-neg.parquet"
    shell: "python markup/schemaorg_examples_dataset.py generate-negative-examples {input} {output} --comp-check"

rule build_semantic_pos:
    output: ancient(f"{SCHEMAORG_DIR}/compliance-pos.parquet")
    shell: "python markup/schemaorg_examples_dataset.py create-dataset {output}"