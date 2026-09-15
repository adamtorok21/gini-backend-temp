import pandas as pd

def clean_dataframe(data):
    df = pd.DataFrame(data[1:], columns=data[0])
    df.columns = [col.strip() for col in df.columns]
    df = df.replace(r"^\s+|\s+$", "", regex=True) 
    df.dropna(how="all", inplace=True)

    return df

