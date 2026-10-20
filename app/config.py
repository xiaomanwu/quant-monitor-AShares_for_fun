"""
交易参数配置：所有成本、风控、回测默认值集中在此，便于调整与审计。
A股实际规则已内置默认值。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field


@dataclass
class CostModel:
    commission_rate: float = 0.00025    # 佣金 万2.5（双边）
    min_commission: float = 5.0        # 单笔最低佣金
    stamp_tax: float = 0.0005          # 印花税，仅卖出
    transfer_fee: float = 0.00001      # 过户费 0.001%（双边）
    slippage: float = 0.001            # 滑点 0.1%（按成交额比例）
    impact_ratio: float = 0.10         # 单日成交量占比上限（冲击成本约束）

    def buy_cost(self, amount: float) -> float:
        comm = max(amount * self.commission_rate, self.min_commission)
        return comm + amount * self.transfer_fee + amount * self.slippage

    def sell_cost(self, amount: float) -> float:
        comm = max(amount * self.commission_rate, self.min_commission)
        return comm + amount * self.transfer_fee + amount * self.stamp_tax + amount * self.slippage

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20       # 单票最大权重
    max_holdings: int = 10               # 最大持仓数
    max_industry_pct: float = 0.40       # 单行业最大权重（无行业数据时按代码前缀近似）
    stop_loss: float = 0.12              # 个股止损线（相对成本）
    trailing_stop: float = 0.15          # 移动止损（从最高点回撤）
    max_drawdown_stop: float = 0.20      # 组合回撤熔断线
    drawdown_reduce_ratio: float = 0.5   # 触发熔断后降至该仓位比例
    volatility_target: float = 0.0       # >0 时启用波动率目标化（年化）
    cash_buffer: float = 0.02            # 现金缓冲，防止成交失败

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestConfig:
    start: str = "2023-01-01"
    end: str = "2026-10-19"
    initial_cash: float = 1_000_000.0
    rebalance_days: int = 5              # 调仓周期（交易日）
    top_n: int = 8                       # 选股数量
    weighting: str = "equal"             # equal | score | vol_inverse | risk_parity
    benchmark: str = "sh000300"          # 基准
    cost: CostModel = field(default_factory=CostModel)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


DEFAULT_BACKTEST = BacktestConfig()

# 策略元信息（前端展示用）
STRATEGY_META = {
    "signal_score": {
        "name": "信号评分选股",
        "desc": "复用监控台 12 条技术信号，汇总为 -100~100 评分，选评分最高的 N 只",
        "tag": "多因子打分",
    },
    "ma_cross": {
        "name": "双均线交叉",
        "desc": "MA 快线上穿慢线买入，下穿卖出，经典趋势跟踪",
        "tag": "趋势跟踪",
    },
    "momentum": {
        "name": "动量轮动",
        "desc": "按 N 日涨幅排序，买入最强标的，定期轮动",
        "tag": "动量",
    },
    "boll_revert": {
        "name": "布林带均值回归",
        "desc": "跌破下轨买入、回归中轨卖出，捕捉超跌反弹",
        "tag": "均值回归",
    },
    "macd_trend": {
        "name": "MACD 趋势",
        "desc": "DIF 上穿 DEA 且位于零轴上方买入，死叉卖出",
        "tag": "趋势跟踪",
    },
}

# 策略默认参数
STRATEGY_PARAMS = {
    "signal_score": {"min_score": 20},
    "ma_cross": {"fast": 5, "slow": 20},
    "momentum": {"lookback": 20, "skip": 0},
    "boll_revert": {"period": 20, "k": 2.0},
    "macd_trend": {"fast": 12, "slow": 26, "signal": 9},
}

# 股票池预设
UNIVERSE_PRESETS = {
    "csi300": {"name": "沪深300", "desc": "大盘蓝筹"},
    "csi500": {"name": "中证500", "desc": "中盘成长"},
    "all_a": {"name": "全A股", "desc": "剔除ST/北交所/新股"},
    "watchlist": {"name": "我的自选", "desc": "监控台自选股"},
}
