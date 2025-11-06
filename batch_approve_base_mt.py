from __future__ import annotations
from typing import Optional, Iterable

import json
import time
import random
from pathlib import Path
from typing import Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from colorama import Fore, Style

import requests
from web3 import Web3
from web3.providers.rpc import HTTPProvider

# ========= 基本配置 =========
RPC_URL = "https://mainnet.base.org"              # Base 主网 RPC
TOKEN_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"                 # 需要授权且作为 investment token 的 ERC20
TOKEN_DECIMALS = 6                                 # 代币精度(如USDC/USDT=6, 大部分=18)
PRIVATE_KEYS_FILE = "private_keys.txt"
PROXIES_FILE = "proxies.txt"                       # 代理列表(可选)

CHAIN_ID = 8453
MAX_WORKERS = 24
REQ_TIMEOUT = 30

# 授权相关
DO_APPROVE = True
GAS_LIMIT_APPROVE = 120000
USE_MAX_ALLOWANCE = True
MAX_UINT256 = 2**256 - 1
ALLOWANCE_THRESHOLD = MAX_UINT256 // 2
CHECK_ALLOWANCE = True                             # 建议大量并发时关闭以防 429

# buy 调用参数（全局默认，可在运行时输入覆盖）
DO_BUY = True
GAS_LIMIT_BUY = 250000                              # 视合约复杂度调整
BUY_INVESTMENT_HUMAN = 0.1                       # 人类可读金额（如 100 USDC）
BUY_OUTCOME_INDEX = 0
BUY_MIN_OUTCOME_TOKENS = 0                          # 最小接收量（未知则设0）

# 发送重试 & 限速
SEND_RETRIES = 2
RETRY_SLEEP = 5
SLEEP_BETWEEN_TX = 0.8                          # 同一钱包内每笔之间间隔+抖动
# ===========================

# ====== 最小 ABI ======
ERC20_ABI = [
    {"constant": False, "inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"approve","outputs":[{"name":"success","type":"bool"}],"type":"function"},
    {"constant": True, "inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],
     "name":"allowance","outputs":[{"name":"remaining","type":"uint256"}],"type":"function"},
{
    "constant": True,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "type": "function"
}
]

# 只包含 buy 方法即可编码 data 和发交易
MARKET_ABI = [
    {"inputs":[
        {"internalType":"uint256","name":"investmentAmount","type":"uint256"},
        {"internalType":"uint256","name":"outcomeIndex","type":"uint256"},
        {"internalType":"uint256","name":"minOutcomeTokensToBuy","type":"uint256"}],
     "name":"buy","outputs":[], "stateMutability":"nonpayable","type":"function"}
]

ALL_MARKET = {}

# ====== 工具函数 ======
def load_json_map(path: str) -> Dict[str, int]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"price map 文件未找到: {path}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def load_private_keys(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"私钥文件未找到: {path}")
    keys = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if not s.startswith("0x"):
                s = "0x" + s
            keys.append(s)
    return keys

def load_proxies(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        return []
    proxies = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u and not u.startswith("#"):
                proxies.append(u)
    return proxies

def fetch_markets_for_oracle(oracle_id: int) -> List[str]:
    url = f"https://api.limitless.exchange/markets/prophet?priceOracleId={oracle_id}&frequency=hourly"
    for attempt in range(1, 4):
        try:
            r = requests.get(url,timeout=3)
            r.raise_for_status()
            data = r.json()
            ALL_MARKET[oracle_id] = data['market']['address']
            break
        except Exception as e:
            if attempt < 3:
                time.sleep(1.0 * attempt)
            else:
                print(f"⚠️ 获取 markets 失败 id={oracle_id}: {e}")

def ensure_checksum_list(w3: Web3, addresses: List[str]) -> List[str]:
    res = []
    for a in addresses:
        try:
            res.append(w3.to_checksum_address(a))
        except Exception:
            print(f"  ⚠️ 非法地址跳过: {a}")
    return list(dict.fromkeys(res))

def make_w3_with_proxy(proxy_url: str | None) -> Web3:
    if proxy_url:
        provider = HTTPProvider(RPC_URL, request_kwargs={"proxies":{"http":proxy_url,"https":proxy_url}, "timeout":REQ_TIMEOUT})
    else:
        provider = HTTPProvider(RPC_URL, request_kwargs={"timeout":REQ_TIMEOUT})
    w3 = Web3(provider)
    if not w3.is_connected():
        raise RuntimeError(f"❌ 无法连接 RPC（proxy={proxy_url or 'DIRECT'}）")
    return w3

def to_smallest_unit(amount_human: float, decimals: int) -> int:
    return int(amount_human * (10 ** decimals))

def allowance_enough(token_contract, owner: str, spender: str) -> bool:
    try:
        val = token_contract.functions.allowance(owner, spender).call()
        return int(val) >= ALLOWANCE_THRESHOLD
    except Exception as e:
        print(f"    ⚠️ allowance 查询失败（将直接发授权）: {e}")
        return False

def send_raw_with_retry(w3: Web3, raw: bytes):
    last_err = None
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return w3.eth.send_raw_transaction(raw)
        except Exception as e:
            last_err = e
            sleep_s = RETRY_SLEEP * attempt + random.random()
            print(f"    ⚠️ 发送失败({attempt}/{SEND_RETRIES}): {e}，{sleep_s:.1f}s 后重试")
            time.sleep(sleep_s)
    raise last_err

def _prepare_oracle_map_from_markets(market_addresses: Iterable[str]) -> Dict[int, List[str]]:
    """
    把 market 地址列表包装成 wallet_worker 可接收的 spenders_by_oracle 结构：
    使用 key 0 (占位)，value 为传入的地址列表（已去重）。
    """
    uniq = []
    for a in market_addresses:
        if not a:
            continue
        if a not in uniq:
            uniq.append(a)
    return {0: uniq}

# ====== 钱包线程 ======
def wallet_worker(pk: str, spenders_by_oracle: Dict[int, List[str]], token_addr: str,
                  buy_amount_smallest: int, buy_outcome_index: int, buy_min_tokens: int,
                  proxy_url: str | None) -> Tuple[str, int, int, int]:
    """
    返回: (wallet_addr, sent_approves, sent_buys, skipped_approves)
    """
    w3 = make_w3_with_proxy(proxy_url)
    acct = w3.eth.account.from_key(pk)
    addr = acct.address
    token = w3.eth.contract(address=w3.to_checksum_address(token_addr), abi=ERC20_ABI)

    try:
        nonce = w3.eth.get_transaction_count(addr, block_identifier="pending")
    except Exception as e:
        print(f"[{addr[:6]}] ❌ 获取 nonce 失败（proxy={proxy_url or 'DIRECT'}）：{e}")
        return addr, 0, 0, 0

    print(f"[{addr[:6]}] 开始（proxy={proxy_url or 'DIRECT'}），初始 nonce={nonce}")

    sent_approve = 0
    sent_buy = 0
    skipped_approve = 0

    # 遍历每个 oracle 的每个 market 地址
    for oracle_id, spenders in spenders_by_oracle.items():
        for market_addr in spenders:
            market_cs = w3.to_checksum_address(market_addr)
            token_balance = token.functions.balanceOf(addr).call()
            human_balance = token_balance / (10 ** TOKEN_DECIMALS)
            human_need = buy_amount_smallest / (10 ** TOKEN_DECIMALS)

            if token_balance < buy_amount_smallest:
                print(f"[{addr[:6]}] ⚠️ 余额不足，当前余额 {int(human_balance)}，购买需要 {int(human_need)}")
                continue

            # 1) 授权
            if DO_APPROVE:
                if CHECK_ALLOWANCE and allowance_enough(token, addr, market_cs):
                    print(f"[{addr[:6]}] 跳过授权（已足够）→ {market_cs}")
                    skipped_approve += 1
                else:
                    try:
                        gas_price = w3.eth.gas_price
                        amount = MAX_UINT256 if USE_MAX_ALLOWANCE else ALLOWANCE_THRESHOLD
                        approve_tx = token.functions.approve(market_cs, amount).build_transaction({
                            "from": addr,
                            "nonce": nonce,
                            "gas": GAS_LIMIT_APPROVE,
                            "gasPrice": gas_price,
                            "chainId": CHAIN_ID
                        })
                        signed = w3.eth.account.sign_transaction(approve_tx, private_key=pk)
                        tx_hash = send_raw_with_retry(w3, signed.raw_transaction)
                        print(f"[{addr[:6]}] ✅ APPROVE https://basescan.org/tx/0x{tx_hash.hex()} -> {market_cs} (nonce={nonce})")
                        # 等待链上确认
                        try:
                            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=5, poll_latency=3)
                            if receipt.status == 1:
                                nonce += 1
                                sent_approve += 1
                            else:
                                print(
                                    f"[{addr[:6]}] ❌ APPROVE failed (status=0): https://basescan.org/tx/0x{tx_hash.hex()}")
                        except Exception as e:
                            print(f"[{addr[:6]}] ⚠️ APPROVE error waiting receipt: {e}")
                    except Exception as e:
                        print(f"[{addr[:6]}] ❌ 授权失败 {market_cs}: {e}")
                        time.sleep(0.5)

            # 2) buy 交易
            if DO_BUY:
                try:
                    gas_price = w3.eth.gas_price
                    market = w3.eth.contract(address=market_cs, abi=MARKET_ABI)

                    buy_tx_data = market.functions.buy(
                        buy_amount_smallest,
                        buy_outcome_index,
                        buy_amount_smallest
                    ).build_transaction({"from": addr})["data"]
                    # 你也可以用 data 验证：print("input=", data)

                    buy_tx = {
                        "to": market_cs,
                        "from": addr,
                        "data": buy_tx_data,
                        "value": 0,                     # buy 非payable，通常为0
                        "nonce": nonce,
                        "gas": GAS_LIMIT_BUY,
                        "gasPrice": gas_price,
                        "chainId": CHAIN_ID
                    }
                    signed = w3.eth.account.sign_transaction(buy_tx, private_key=pk)
                    tx_hash = send_raw_with_retry(w3, signed.raw_transaction)
                    print(f"[{addr[:6]}] 🟩 BUY https://basescan.org/tx/0x{tx_hash.hex()} -> outcome={buy_outcome_index}, invest={buy_amount_smallest} (nonce={nonce})")
                    nonce += 1
                    sent_buy += 1
                    time.sleep(SLEEP_BETWEEN_TX + random.random()*0.4)
                except Exception as e:
                    print(f"[{addr[:6]}] ❌ BUY 失败 {market_cs}: {e}")
                    time.sleep(0.5)

    print(f"[{addr[:6]}] 完成：approve={sent_approve}, buy={sent_buy}, skipped_approve={skipped_approve}")
    return addr, sent_approve, sent_buy, skipped_approve

# ====== 主流程 ======
def main():
    # 读取配置 & 输入
    price_map = {
      "SOL": 59,
      "BNB":61,
      "ETH": 58,
      "DOGE": 60,
      "XRP": 62
    }
    priv_keys = load_private_keys(PRIVATE_KEYS_FILE)
    proxies = load_proxies(PROXIES_FILE)

    items = list(price_map.items())
    print("\n🔍 正在获取市场地址 …")
    # ====== 并发获取市场地址 ======
    with ThreadPoolExecutor(max_workers=10) as executor:  # 可根据网络调整
        futures = {executor.submit(fetch_markets_for_oracle, oid): (sym, oid) for sym, oid in items}

        for future in as_completed(futures):
            sym, oid = futures[future]
            try:
                future.result()
            except Exception as e:
                pass


    # 选择币种
    print("📜 可选币种：")
    for i, (sym, oid) in enumerate(items, 1):
        print(f"{i}. {sym} (priceOracleId={oid} priceContract={ALL_MARKET[oid]})")
    choice = input("\n请输入要操作的币种序号: ").strip()

    selected: List[Tuple[str,int]] = []
    for part in choice.split(","):
        p = part.strip().upper()
        if not p: continue
        if p.isdigit():
            idx = int(p)
            if 1 <= idx <= len(items):
                selected.append(items[idx-1])
        elif p in price_map:
            selected.append((p, price_map[p]))
    if not selected:
        print("❌ 未选择有效币种，退出。"); return

    print("\n✅ 选中的币种：")
    for s, oid in selected:
        print(f"- {s} (id={oid})")

    # buy 参数可在运行时覆盖
    try:
        h = input(f"\n投资金额 : ").strip()
        if h:
            hval = float(h)
        else:
            hval = BUY_INVESTMENT_HUMAN
    except:
        hval = BUY_INVESTMENT_HUMAN

    print("📊 请选择市场方向：")
    print(" 涨涨涨⬆️ [0] 📈 ")
    print(" 跌跌跌⬇️ [1] 📉 ")
    # choice = input("请输入选项编号 (0 或 1)：").strip()
    oi =  input("请输入选项编号 (0 或 1)：").strip()
    if oi not in ("0", "1"):
        print(f"{Fore.YELLOW}⚠️ 请输入 0（涨） 或 1（跌）{Style.RESET_ALL}")
        exit()
    if oi:
        try: BUY_outcome_index = int(oi)
        except: BUY_outcome_index = BUY_OUTCOME_INDEX
    else:
        BUY_outcome_index = BUY_OUTCOME_INDEX

    if oi == "0":
        sure = input(f"{Fore.GREEN}购买涨📈 确定吗 Y/N? : ").strip()
    else:
        sure = input(f"{Fore.RED}购买跌📉 确定吗 Y/N? : ").strip()

    if sure.lower() != "y":
        print(f"{Fore.YELLOW}取消操作{Style.RESET_ALL}")
        return


    # mot = input(f"minOutcomeTokensToBuy（默认 {BUY_MIN_OUTCOME_TOKENS} ）: ").strip()
    # if mot:
    #     try: BUY_min_tokens = int(mot)
    #     except: BUY_min_tokens = BUY_MIN_OUTCOME_TOKENS
    # else:
    BUY_min_tokens = BUY_MIN_OUTCOME_TOKENS
    mot = BUY_MIN_OUTCOME_TOKENS
    buy_amount_smallest = to_smallest_unit(hval, TOKEN_DECIMALS)
    print(f"\n→ buy 参数：investmentAmount={buy_amount_smallest} (decimals={TOKEN_DECIMALS}), outcomeIndex={BUY_outcome_index}, minOutcomeTokensToBuy={BUY_min_tokens}")

    # 先用直连 w3 做地址校验
    w3_global = make_w3_with_proxy(None)

    # 拉取 spender 列表
    oracle_to_spenders: Dict[int, List[str]] = {}
    all_spenders: List[str] = []
    print("\n🔍 正在获取市场地址 …")
    for sym, oid in selected:
        addrs: List[str] = [Web3.to_checksum_address(ALL_MARKET[oid])]
        oracle_to_spenders[oid] = addrs
        all_spenders.extend(addrs)
        print(f"  {sym}: {len(addrs)} 个地址")
    uniq_spenders = list(dict.fromkeys(all_spenders))
    print(f"\n即将操作的合约地址（ {len(uniq_spenders)} 个）：")
    for a in uniq_spenders:
        print("  ➜", a)

    # yn = input("\n确认继续（approve→buy）？(Y/n): ").strip().lower()
    # if yn and yn not in ("y","yes"):
    #     print("已取消。"); return

    # 分配代理（按钱包索引轮询）
    def proxy_for_index(i: int) -> str | None:
        return None if not proxies else proxies[(i-1) % len(proxies)]

    print(f"\n🚀 并发执行：{len(priv_keys)} 个钱包，max_workers={MAX_WORKERS}，代理源={len(proxies) or '无'}")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = []
        for idx, pk in enumerate(priv_keys, 1):
            futs.append(ex.submit(
                wallet_worker, pk, oracle_to_spenders, TOKEN_ADDRESS,
                buy_amount_smallest, BUY_outcome_index, BUY_min_tokens,
                proxy_for_index(idx)
            ))
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                print(f"线程异常：{e}")

    total_app = sum(r[1] for r in results)
    total_buy = sum(r[2] for r in results)
    total_skip = sum(r[3] for r in results)
    print("\n====== 汇总 ======")
    print(f"钱包数：{len(results)}")
    print(f"总 approve：{total_app}")
    print(f"总 buy：{total_buy}")
    print(f"跳过的 approve：{total_skip}")
    print("完成。")

def start_by_address(address: str, buy_investment_human: float, buy_outcome_index: int):
    """
    最简便的外部调用入口（只传单个 market 地址 + buy 参数）
    示例：
        from your_script import start_by_address
        start_by_address("0xMarketAddr...", 0.1, 0)
    """
    if not address:
        raise ValueError("address 不能为空")
    # 如果传进来是单地址字符串，包装成 list
    markets = [address]
    return run_for_markets(markets, TOKEN_ADDRESS, buy_investment_human, buy_outcome_index)

def run_for_markets(
    market_addresses: List[str],
    token_address: str,
    buy_investment_human: float,
    buy_outcome_index: int,
    proxies: Optional[List[str]] = None,
    max_workers: Optional[int] = None
):
    """
    非交互入口：直接针对传入的 market_addresses 并发执行 wallet_worker。
    - market_addresses: list of market contract addresses (str)
    - token_address: ERC20 token 地址（若传空则使用脚本顶部 TOKEN_ADDRESS）
    - buy_investment_human: 人类可读金额（float），会转换为最小单位
    - buy_outcome_index: outcome index (int)
    - proxies: 可选代理列表（若 None 则使用脚本读取的 proxies.txt）
    - max_workers: 可选线程池大小（默认使用 MAX_WORKERS）
    返回： results 列表（每个线程返回 wallet_worker 的元组）
    """
    # 使用传入参数覆盖全局配置（局部化）
    token_addr = token_address or TOKEN_ADDRESS
    decimals = TOKEN_DECIMALS
    buy_amount_smallest = to_smallest_unit(buy_investment_human, decimals)
    buy_index = int(buy_outcome_index)

    # 代理分配器
    local_proxies = proxies if proxies is not None else load_proxies(PROXIES_FILE)
    def proxy_for_index(i: int) -> str | None:
        return None if not local_proxies else local_proxies[(i-1) % len(local_proxies)]

    spenders_map = _prepare_oracle_map_from_markets(market_addresses)
    workers = load_private_keys(PRIVATE_KEYS_FILE)
    if not workers:
        raise RuntimeError("未加载到私钥，请检查 PRIVATE_KEYS_FILE")

    use_max_workers = max_workers or MAX_WORKERS
    results = []
    print(f"▶ run_for_markets: markets={len(market_addresses)}, wallets={len(workers)}, workers={use_max_workers}")
    with ThreadPoolExecutor(max_workers=use_max_workers) as ex:
        futs = []
        for idx, pk in enumerate(workers, 1):
            futs.append(ex.submit(
                wallet_worker,
                pk,
                spenders_map,
                token_addr,
                buy_amount_smallest,
                buy_index,
                BUY_MIN_OUTCOME_TOKENS,
                proxy_for_index(idx)
            ))
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                print(f"线程异常：{e}")
    return results

if __name__ == "__main__":
    main()
