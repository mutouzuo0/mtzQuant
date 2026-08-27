

### 🔍 第一步：需求解构——精准定义策略骨架  
**目标**：将模糊的交易想法转化为结构化需求

#### 📌提示词举例
```markdown 
当前策略需求：实现一个xxx买卖策略
请从监控标的、使用数据、指标计算、买卖规则等维度完善具体要求，每个维度一句话概括。

注意：1、不支持盘后下单；2、暂不支持行业、概念板块数据

```



---

### 🧩 第二步：策略构建——Deepseek 自动生成代码  
**目标**：利用模板引擎一键生成策略框架  

#### 📌 上传知识库
《PTrade所有API函数接口清单》
#### 📌提示词举例
```python
请严格遵循以下规范生成PTrade策略代码：
一、策略框架约束
ptrade量化引擎以事件触发为基础，通过初始化事件（initialize）、盘前事件（before_trading_start）、盘中事件（handle_data）、盘后事件（after_trading_end）来完成每个交易日的策略任务。
## 1、初始化initialize(context)
用于初始化一些全局变量，这是必选步骤。
## 2、盘前before_trading_start(context, data)
每天开始交易前被调用，可在此添加每天都要初始化的信息，缺省时间为 9:10，是可选步骤。
## 3、盘中
### （1）日线级别handle_data(context, data)
每日执行一次，日策策略的启动时间设为收盘前，缺省时间是 14:50，为必选。
### （2）分钟级别handle_data(context, data)
每分钟执行一次，分钟周期的启动时间为 09:30，是必选。如每隔多分钟运行一次或每日固定时间运行，可使用context.blotter.current_dt.time()判断时间后再执行逻辑。
### （3）tick 级别tick_data(context, data)、run_interval(context, func, seconds=10)
tick 级别可以通过 tick_data 或 run_interval 这两个 API 实现，tick_data 每 3 秒执行一次，run_interval 的间隔可自主设定，最小周期是 3 秒，是可选。
## 4、盘后after_trading_end(context, data)
每天交易结束之后调用，用来处理每天收盘后的事务，缺省时间为 15:30，是可选步骤。
二、API约束
禁止使用任何未在《PTrade所有API函数接口清单》中出现的函数

三、策略示例
# 每日收盘运行示例
def initialize(context):
    g.security = '600109.SS'
set_universe(g.security)
def before_trading_start(context, data):
    pass
def handle_data(context, data):
    security = g.security
    df = get_history(10, '1d', 'close', security, fq=None, include=False) #能使用get_history就不要使用get_price
    ma5 = round(df['close'][-5:].mean(), 3)
    ma10 = round(df['close'][-10:].mean(), 3)
    price = data[security]['close']
    cash = context.portfolio.cash
    if ma5 > ma10:
        order_value(security, cash)
        log.info("Buying %s" % (security))
    elif ma5 < ma10 and get_position(security).amount > 0:
        order_target(security, 0)
        log.info("Selling %s" % (security))
def after_trading_end(context, data):
    pass

# 盘中定时运行示例
def initialize(context):
    g.security = '600109.SS'
set_universe(g.security)
def before_trading_start(context, data):
    pass
def handle_data(context, data):
    current_time = context.blotter.current_dt.time()
    if current_time.hour != 9 or current_time.minute != 31:
        return  # 不是交易时间，直接退出
    else:
        security = g.security
        df = get_history(10, '1d', 'close', security, fq=None, include=False) 
        ma5 = round(df['close'][-5:].mean(), 3)
        ma10 = round(df['close'][-10:].mean(), 3)
        price = data[security]['close']
        cash = context.portfolio.cash
        if ma5 > ma10:
            order_value(security, cash)
            log.info("Buying %s" % (security))
        elif ma5 < ma10 and get_position(security).amount > 0:
            order_target(security, 0)
            log.info("Selling %s" % (security))
四、当前需求
实现一个{XXX}买卖策略，具体要求：
1. 监控标的：策略监控单一股票或一组股票池中的标的，通过设置股票池来确定交易范围
2. 使用数据：策略使用每日的收盘价和成交量数据来计算XXX指标
3. 指标计算：XXX通过累计每日成交量的变化来反映市场的买卖力量，计算公式为：xxxxxx
4. 买卖规则：设定XXX指标的短期均线（如5日均线）和长期均线（如20日均线）进行交叉判断，当短期均线向上穿过长期均线时买入，向下穿过时卖出。
5. ...
```


---

### 🔎 第三步：规范核查——自我校正，减少幻觉  
**目标**：一键扫描规避 API 误用与结构错误，修复大模型幻觉

#### 📌提示词举例
```markdown 
以批判的角度检查策略代码及注释是否符合PTrade规范，依次确认以下要求，如需修改请输出完整策略代码：
1. 设置函数参数用法及含义是否符合《API接口明细-设置函数》规范
检查内容：
检查所有设置函数的参数是否与文档中的定义一致，包括参数名称、类型、顺序和默认值。
确保命名参数的使用正确，避免参数顺序错误。
示例：
set_commission(commission_ratio=0.0003, min_commission=3.0)  # 正确
set_commission(0.0003, 0.0003)  # 错误，参数顺序不明确
2. 数据获取函数参数用法及含义是否符合《API接口明细-获取信息函数》规范
检查内容：
（1）完整列出代码中使用的数据获取函数名称
（2）逐一检查所有数据获取函数是否在回测模块可用，所有参数用途是否与文档中定义用途一致，参数类型、格式是否与文档及函数示例保持一致。
确保参数的使用符合函数的预期行为。
示例：
get_history(10, '1d', 'close', security, fq=None, include=False)  # 正确
get_history(10, '1d', 'close', include=True)  # 错误，security、fq 参数未设置
3. 使用的所有对象、数据字典是否符合《PTrade数据结构》规范
检查内容：
逐一检查代码中使用的对象（如 g、context、data、Portfolio、Position 等）是否符合文档中的定义。
确保对象的属性和方法的使用符合规范。
示例：
context.portfolio.cash  # 正确
context.portfolio.cash_value  # 错误，cash_value 不是 Portfolio 的属性
4. 股票代码尾缀是否符合规范
检查内容：
检查所有股票代码的尾缀是否符合以下规范：
市场品种	尾缀全称	尾缀简称
上海市场证券	XSHG	SS
深圳市场证券	XSHE	SZ
指数	XBHS	
示例：
g.security = '600109.SS'  # 正确
g.security = '600109'      # 错误，缺少尾缀
5. log.info() 用法是否符合规范
检查内容：
检查 log.info() 的用法是否符合以下格式：
log.info("输出字符串 %s" % (变量名))
确保使用 %s 占位符并传入变量，避免直接拼接字符串。
如有选股逻辑，检查是否通过log.info输出选股结果。
示例：
log.info("Buying %s" % (security))  # 正确
log.info("Buying " + security)       # 错误，直接拼接字符串
6. 数据类型和数据结构是否正确
检查内容：
确保函数返回的数据类型（如 DataFrame、Series、list 等）与文档一致。
确保数据结构的使用符合规范。
示例：
df = get_history(10, '1d', 'close', security_list=None)  # 返回 DataFrame
series = df['close']  # 返回 Series
list_obj = get_history(10, '1d', 'close', security_list=None)  # 错误，期望返回 DataFrame

```
**💡 技巧**：上传物料《API接口明细-设置函数》《API接口明细-获取信息函数》《PTrade数据结构》至知识库  

---

### 🐞 第四步：测试修正——快速定位与修复 BUG
**目标**：从报错日志到完美运行  

#### 📌 实战流程  
1. **编译回测**：PTrade 回测引擎运行  
2. **报错定位**：定位日志中的 `Traceback` 信息，复制从“错误/Exception: ：Traceback (most recent call last):”到“xxxError:xxxx”之间的所有行
3. **BUG修复**：将报错粘贴至 Deepseek → 获取修正代码  （tips:粘贴完报错后，补充一句“修正并返回完整代码”）

**📂 案例**  
```python  
# 报错：AttributeError: 'Portfolio' object has no attribute 'cash_value'  
# 修正后  
cash = context.portfolio.cash  # ✅ 正确属性  
```
**💡 注意**：该步骤可能存在修复后仍有报错情形，需耐心完成下一轮交互，直到策略可正常运行。

---

### 📈 第五步：绩效优化——参数调优与压力测试  
**目标**：让策略表现更加稳定，适应不同市场环境  

**参考提示词**：“逐行增加注释，明确小白可修改之处，并将所有小白可修改的参数集中到初始化函数”  

```python  
def initialize(context):  
    g.ma_short = 5    # 🎯 小白可修改此处！  
    g.ma_long = 20  
```



---

### 🚀 第六步：落地实战——从小资金到实盘验证  
**目标**：低风险验证策略有效性  

####  操作指南  
1. **模拟盘测试**
2. **实盘启动**
3. **监控日志**

---

### 🔄 第七步：持续改进——迭代升级策略  
**目标**：动态适应市场变化

####  迭代思路
1. **信号增强**
2. **风控模块**
3. **AI 进化**


