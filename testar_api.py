from arquivos import carregar, status_fonte

try:
    df = carregar()
    print("Conexão JT API: OK")
    print("Fonte:", status_fonte())
    print(f"Linhas agregadas carregadas: {len(df):,}")
    if not df.empty:
        print("Datas:", df["data"].min(), "até", df["data"].max())
        print("Indicadores:", sorted(df["indicador"].dropna().unique().tolist()))
except Exception as exc:
    print("Falha ao consultar a JT API:")
    print(exc)
    raise
