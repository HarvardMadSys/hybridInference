import pandas as pd


def load_burst_trace(file_path: str):
    df = pd.read_csv(file_path)
    df = df[df["Log Type"] != "Conversation log"]
    # df['Timestamp'] = df['Timestamp'] / 10
    df = pd.DataFrame(df.values.repeat(20, axis=0), columns=df.columns).reset_index(drop=True)
    print(df.head())
    return df


import yaml


def load_infra_config(path="infra_config.yaml"):
    with open(path) as f:
        config = yaml.safe_load(f)
    return config
