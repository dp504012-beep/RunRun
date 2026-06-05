# crypto-backtester

本專案是本地執行的加密策略回測工具骨架。

目前只提供專案結構、CLI 入口、日志設定、基本例外類別與測試環境。
不包含 Binance API、交易策略、Google Sheets 寫入或任何網站介面。

## 需求

- Python 3.11+

## 安裝

```bash
python -m pip install -e .[dev]
```

## 執行 CLI

```bash
python -m src.cli --help
python -m src.cli update-data
python -m src.cli run
python -m src.cli export-sheets
python -m src.cli validate-data
```

目前各指令都只會回傳「尚未實作」。

## 測試

```bash
pytest
```

## 專案結構

```text
crypto-backtester/
├── config/
├── data/
│   └── parquet/
├── reports/
├── src/
│   ├── data/
│   ├── engine/
│   ├── strategies/
│   ├── metrics/
│   └── outputs/
├── tests/
├── prompts/
├── pyproject.toml
├── README.md
├── .env.example
└── .gitignore
```

## 尚未實作

- 歷史 K 線下載
- 回測引擎
- 交易策略
- 指標計算
- Google Sheets 輸出

