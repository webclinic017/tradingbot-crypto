from fastapi import FastAPI, HTTPException, Depends
from db import SessionLocal, engine, Base, \
    AccountPD, TradePD, ErrorPD, PriceHistoryPD, \
    Account, Trade, Error, PriceHistory, ApeRank
from sqlalchemy.orm import Session
from binance import Client
from typing import List, Dict
from os import environ
import pandas as pd
from dotenv import load_dotenv
load_dotenv() 
from requests import get, post
from datetime import datetime, timedelta
import uvicorn
from hashlib import sha512
import json

Base.metadata.create_all(bind=engine)


# Dependency
def get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()


app = FastAPI()

binanceapi = Client(environ["BINANCE_KEY"], environ["BINANCE_SECRET"])
environ["SYMBOLS"] = "BTC,ETH,MATIC,POL,AVAX,XRP,BNB,LINK,ADA"
SYMBOLS = environ["SYMBOLS"].split(",")
SYMBOLS = environ["SYMBOLS"].split(",")
SYMBOLS = [symb + "USDT" for symb in SYMBOLS]

# @app.on_event("startup")
# def startup_event():
    # also check with DB to make sure we have everything
    # uniqueSymbols = pricehistoryDB.distinct("symbol")
    # for symbol in uniqueSymbols:
    #     if symbol not in SYMBOLS:
    #         try:
    #             # pricehistoryDB.insert_many(getHistoricPrices(symbol).to_dict("records"))
    #             SYMBOLS.append(symbol) # add that symbol from the db
    #         except Exception as e:
    #             # print(e)
    #             print("startup: Failed to get historic prices for symbol: " + symbol)


# checks if account exists and returns it if it does
def getAccount(name: str, db):
    # db.query(models.Record).all()
    account = db.query(Account).filter(Account.name == name).first()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account

@app.put("/accounts/{name}")
def create_account(name: str, description: str = "", startmoney: float = 10000., db: Session = Depends(get_db)):
    # check if account exists
    account = db.query(Account).filter(Account.name == name).first()
    if account is not None:
        raise HTTPException(status_code=409, detail="Account already exists")
    # create account
    portfolio = {
        "USDT": startmoney,
    }
    account = Account(name=name, portfolio=portfolio, description=description, 
        lastTrade=datetime.utcnow(), netWorth = startmoney, lastUpdateWorth = datetime.utcnow())
    db.add(account)
    db.commit()
    return account

@app.get("/accounts/")
def getAccounts(db: Session = Depends(get_db)):
    return db.query(Account.name).all()

@app.get("/accounts/{name}")
def get_account(name: str, db: Session = Depends(get_db)):
    return getAccount(name, db)

@app.get("/portfolio/{name}", response_model = Dict[str, float])
def get_portfolio(name: str, db: Session = Depends(get_db)):
    account = getAccount(name, db)
    return account.portfolio

@app.get("/symbolcheck/{symbol}")
def check_symbol(symbol: str):
    try:
        info = binanceapi.get_symbol_info(symbol)
        if info is None:
            return False
        else:
            return info
    except:
        return False

def createHistoricPriceId(row):
    return sha512((row["symbol"] + str(row["opentime"])).encode()).hexdigest()[:10]

def getHistoricPrices(symbol, interval = "60m", lookback = "2 hour ago UTC"):
    if interval == "1m":
        interval = Client.KLINE_INTERVAL_1MINUTE
    elif interval == "60m":
        interval = Client.KLINE_INTERVAL_1HOUR
    elif interval == "30m":
        interval = Client.KLINE_INTERVAL_30MINUTE
    elif interval == "1d":
        interval = Client.KLINE_INTERVAL_1DAY
    else:
        raise ValueError("Invalid interval, only 1m, 30m 60m 1d supported")
    klines = binanceapi.get_historical_klines(symbol, interval, lookback)
    hist_df = pd.DataFrame(klines)
    hist_df.columns = ['opentime', 'open', 'high', 'low', 'close', 'volume', 'Close Time', 'Quote Asset Volume', 
                    'nrTrades', 'tbbasevolume', 'tbquotevolume', 'Ignore']
    hist_df['opentime'] = pd.to_datetime(hist_df['opentime']/1000, unit='s')
    hist_df['Close Time'] = pd.to_datetime(hist_df['Close Time']/1000, unit='s')
    hist_df = hist_df[['opentime', 'open', 'high', 'low', 'close', 'volume','nrTrades', 'tbbasevolume', 'tbquotevolume']]
    for col in ['open', 'high', 'low', 'close','volume','tbbasevolume', 'tbquotevolume']:
        hist_df[col] = hist_df[col].astype(float)
    for col in ['nrTrades']:
        hist_df[col] = hist_df[col].astype(int)
    hist_df["symbol"] = symbol
    # then create id out of it
    hist_df["id"] = hist_df.apply(createHistoricPriceId, axis=1)
    return hist_df

def savePrice2DB(df, db):
    # write only those to DB that are not already in there
    bulk = []
    for i in range(len(df)):
        obj = PriceHistory(**df.iloc[i].to_dict())
        db.merge(obj)
    db.commit()

# @app.get("/update/price")
def update(db):
    print("geddin updates for: ", SYMBOLS)
    for symbol in SYMBOLS:
        try:
            histDF = getHistoricPrices(symbol)
        except Exception as e:
            print("problem with symbol " + symbol  + ": " + str(e))
            continue
        savePrice2DB(histDF, db)

# 
def __updatePortfolioWorth(db):
    symbolPrices = dict()
    allAccounts = db.query(Account).all()
    for account in allAccounts:
        netWorth = account.portfolio.get("USDT", 0.)
        for symbol, amount in account.portfolio.items():
            if symbol != "USDT":
                if symbol not in symbolPrices:
                    try:
                        symbolPrices[symbol] = getCurrentPrice(symbol)
                    except Exception as e:
                        raise Exception("problem with: " + symbol + ": " + str(e))
                    netWorth += symbolPrices[symbol] * amount
        account.netWorth = netWorth
        account.lastUpdateWorth = datetime.utcnow()
        db.commit()

@app.get("/update/portfolioworth")
def updatePortfolioWorth(db: Session = Depends(get_db)):
    __updatePortfolioWorth(db)
        

@app.get("/update/priceBig")
def bigUpdate(symbolSelection = SYMBOLS, db: Session = Depends(get_db)):
    for symbol in symbolSelection:
        try:
            histDF = getHistoricPrices(symbol, lookback="5 year ago UTC")
        except Exception as e:
            print("problem with symbol " + symbol  + ": " + str(e))
            continue
        savePrice2DB(histDF, db)

def apeTickerFix(ticker):
    return ticker.replace(".X", "USDT")

# apewisdom
# supposed to be executed daily
# @app.get("/update/apewisdom/")
def apewisdom(db):
    jsdata = get("https://apewisdom.io/api/v1.0/filter/all-crypto/").json()["results"]
    df = pd.DataFrame(jsdata)
    df.drop(["name", "rank_24h_ago", "mentions_24h_ago"], axis=1, inplace=True)
    df["ticker"] = df["ticker"].apply(apeTickerFix)
    df["timestamp"] = datetime.utcnow()
    for col in ["mentions", "upvotes", "rank"]:
        df[col] = df[col].astype(int)
    for i in range(len(df)):
        obj = ApeRank(**df.iloc[i].to_dict())
        db.merge(obj)
    db.commit()

@app.get("/update/hourly/")
def hourlyUpdate(db: Session = Depends(get_db)):
    update(db) # prices update hourly
    apewisdom(db)
    __updatePortfolioWorth(db)

@app.get("/apewisdom/{ticker}/{lookback}")
def apewisdomGet(ticker: str, lookback: str, db: Session = Depends(get_db)):
    return db.query(ApeRank).filter(ApeRank.ticker == ticker).filter(ApeRank.timestamp > datetime.utcnow() - pd.Timedelta(lookback)).all()

# get price data
@app.get("/priceHistoric/{symbol}/{lookbackdays}")
def getPriceHistoric(symbol: str, lookbackdays: int = 1, db: Session = Depends(get_db)):
    lookback = datetime.utcnow() - timedelta(days=lookbackdays)
    hist = db.query(PriceHistoryPD).filter(PriceHistoryPD.symbol == symbol).filter(PriceHistoryPD.opentime > lookback).all()
    if len(hist) == 0:
        # raise HTTPException(status_code=404, detail="Price history not found")
        # download data for that symbol
        SYMBOLS.append(symbol)
        # trigger update
        bigUpdate([symbol])
        # recursive call of this fct
        return getPriceHistoric(symbol, lookbackdays)

    hist = pd.DataFrame(hist)
    hist = hist.set_index("opentime")
    hist.drop(["id"], axis=1, inplace=True)
    return hist.to_dict()

## trade functioms

def getCurrentPrice(symbol):
    return float(binanceapi.get_avg_price(symbol=symbol)["price"])

COMMISSION = 0.00125
@app.put("/buy/{name}/{symbol}/{amount}")
def buy(name: str, symbol: str, amount: float, db: Session = Depends(get_db)):
    account = getAccount(name, db)
    # portfolio = account.portfolio
    usdt = account.portfolio["USDT"]
    currentPrice = getCurrentPrice(symbol)
    cost = currentPrice * amount * (1 + COMMISSION)
    if cost > usdt:
        raise HTTPException(status_code=400, detail="Not enough USDT. requires: %.2f$, you have %.2f$" % (cost, usdt))
    account.portfolio["USDT"] = usdt - cost
    account.portfolio[symbol] = account.portfolio.get(symbol, 0) + amount
    # account.portfolio = json.dumps(portfolio)
    db.merge(account)
    db.commit()
    return account.portfolio

def __sell(name, symbol, amount, db):
    account = getAccount(name, db)
    # portfolio = account.portfolio
    amountSymbol = account.portfolio.get(symbol, 0)
    if amountSymbol == 0:
        raise HTTPException(status_code=400, detail="No such symbol in portfolio. portfolio: %s" % str(account.portfolio))
    if amount > amountSymbol:
        raise HTTPException(status_code=400, detail="Not enough %s. requires: %.4f, you have %.4f" % (symbol, amount, amountSymbol))
    if amount == -1:
        # means sell all
        amount = amountSymbol
    win = amount * getCurrentPrice(symbol) * (1 - COMMISSION)
    account.portfolio[symbol] = amountSymbol - amount
    account.portfolio["USDT"] = account.portfolio.get("USDT", 0) + win
    # account.portfolio = account.portfolio
    db.merge(account)
    db.commit()
    return account.portfolio

# sell
@app.put("/sell/{name}/{symbol}/{amount}")
def sell(name: str, symbol: str, amount: float = -1, db: Session = Depends(get_db)):
    return __sell(name, symbol, amount, db)

@app.post("/emergencyLiquidate/{name}")
def emergencyLiquidate(name: str, db: Session = Depends(get_db)):
    account = getAccount(name, db)
    portfolio = account.portfolio
    for symbol in portfolio:
        if symbol != "USDT":
            _ = __sell(name, symbol, -1, db)
    return account



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0")