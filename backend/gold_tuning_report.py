"""Build a Chinese audit report and flat workbook input from saved experiments."""
import json
from pathlib import Path
from gold_tuning import dump

ROOT=Path(__file__).parents[1]
OUT=ROOT/'research/output/gold_tuning_2026-10-20'


def main():
    flat=[]
    studies=[json.loads((OUT/'results.json').read_text()),json.loads((OUT/'round2/results.json').read_text())]
    for round_no,study in enumerate(studies,1):
        for panel,targets in study['panels'].items():
            for target,r in targets.items():
                for name,m in r['full'].items():
                    ci=m['simultaneous_ci95'] or [None,None]
                    flat.append([round_no,panel,target,name,m['n'],m['mse'],r['full'][r['primary_baseline']]['mse'],
                                 m['r2_vs_primary'],r['full']['mean']['mse'],m['r2_vs_mean'],*m['fold_r2'],
                                 r['tail26'][name]['r2_vs_primary'],*ci,m['nonoverlap_phase_r2_min']])
    risk=json.loads((OUT/'round3/results.json').read_text())
    for name,m in risk['full'].items():
        ci=m['simultaneous_ci95'] or [None,None]
        flat.append([3,'macro','vol20',name,m['n'],m['mse'],risk['full']['persistence20']['mse'],
                     m['r2_vs_primary'],risk['full']['mean']['mse'],m['r2_vs_mean'],*m['fold_r2'],
                     risk['tail26'][name]['r2_vs_primary'],*ci,m['nonoverlap_phase_r2_min']])
    selected=[(2,'macro','return5','drift/w52/s0.5'),(2,'macro','return20','vol_only/w52/a1/s1'),
              (2,'macro','return60','rates_fx/w0/a0.1/s0.25'),(3,'macro','vol20','ewma/0.97'),
              (3,'macro','vol20','ewma/0.99'),(3,'macro','vol20','ensemble'),
              (2,'macro','drawdown20','market/w52/a10/s1/level')]
    summary=[]
    for key in selected:
        idx=next(i for i,r in enumerate(flat) if tuple(r[:4])==key)
        summary.append({'source_row':idx+6,'key':key})
    neighborhood=[]
    for name,m in risk['full'].items():
        neighborhood.append([name,m['n'],m['r2_vs_primary'],1-m['mse']/risk['full']['persistence60']['mse'],
                             risk['tail26'][name]['r2_vs_primary'],risk['excluding2020'][name]['r2_vs_primary'],
                             risk['trim_top5pct_baseline_errors'][name]['r2_vs_primary'],risk['qlike'][name],
                             *[v[name]['r2_vs_primary'] for v in risk['yearly'].values()]])
    count=sum(len(r['manifest']['configurations'][p])*(3 if p=='score8' else 5) for r in studies[:1] for p in r['manifest']['configurations'])
    count+=192*3+384*2+10
    dump(OUT/'workbook-data.json',{'rows':flat,'summary':summary,'neighborhood':neighborhood,'years':list(risk['yearly']),
                                  'trial_rows':len(flat),'configured_target_combinations':count})
    lines=['# 黄金调参迭代结果（2026-10-20）','',
           '本轮最值得保留的是波动风险先验。收益方向预测暂未找到可稳定采用的方案。', '',
           f'完成三轮有记录实验：约{count:,}个模型参数与目标组合（含跨轮复测和早期等效窗口），连同基准、嵌套选择，共保存{len(flat):,}行完整评价。所有候选保留，未更改生产八指标权重。', '',
           '## 最有希望的方案','',
           '未来20交易日的年化实现波动率，用过去日收益的EWMA方差预测。保留λ=0.97及λ=0.94/0.97/0.99等权方差集合；λ=0.99是本轮事后表现较好的对照。', '',
           '|方案|周预测数|MSE改善 vs过去20日波动|MSE改善 vs过去60日波动|最近26周改善|',
           '|---|---:|---:|---:|---:|']
    for k,label in [('ewma/0.97','EWMA λ=.97'),('ewma/0.99','EWMA λ=.99（事后较优）'),('ensemble','三参数方差集合')]:
        m=risk['full'][k]
        lines.append(f"|{label}|{m['n']}|{m['r2_vs_primary']:.1%}|{1-m['mse']/risk['full']['persistence60']['mse']:.1%}|{risk['tail26'][k]['r2_vs_primary']:.1%}|")
    lines += ['', '共同评估期为2020-05-15至2026-09-18。20日标签重叠，332周点不等于332个独立样本。按实际交易日分离后，每个非重叠相位约74–75个预测。', '',
              'λ=.97–.995构成宽的正改善区间，而非只有一个尖锐最优点。三参数集合的三个时间分段改善35.6%、33.6%、25.2%；排除2020年仍改善27.2%；剔除过去20日波动基准最大5%误差点后改善32.4%。各年均为正，QLIKE也优于过去20/60日波动。', '',
              '集合按方差平均，再开方：sigma²=(sigma²_.94+sigma²_.97+sigma²_.99)/3。EWMA归一化权重为λ^j；sigma=sqrt(252×加权过去日对数收益平方)。不从历史最优权重构造复杂新组合。', '',
              '## 收益预测与回撤预测','',
              '|目标|事后最佳候选相对零收益的MSE改善|按历史已到期表现嵌套选参的改善|最近26周最佳候选改善|',
              '|---|---:|---:|---:|']
    for target in ('return5','return20','return60'):
        r=studies[1]['panels']['macro'][target]
        k=max(r['full'],key=lambda k:r['full'][k]['r2_vs_primary'])
        lines.append(f"|{target}|{r['full'][k]['r2_vs_primary']:.1%}|{r['full']['nested']['r2_vs_primary']:.1%}|{r['tail26'][k]['r2_vs_primary']:.1%}|")
    lines += ['', '收益最优值不能直接采用：5日最优已是简单收缩均值；20/60日最佳候选近期均转负；按过去成熟预测误差选参数的流程也未稳定胜过零收益。原八项分和加入ETF/COT的面板没有在共同日期产生稳定改善。', '',
              '20日回撤幅度的最佳原尺度岭回归相对过去20日回撤改善44.3%，但相对扩展历史均值只改善10.8%；其他参数、嵌套选择与近期并未一致胜过均值，因此列为次级探索，不上升为回撤预警概率。', '',
              '## 为什么还不能叫验证成功','',
              '输入是最新修订版本，发布时点用保守日历代理；原始vintage尚未恢复。整个历史已可被研究者查看，第二、三轮又根据前轮结果修改候选，均为探索性pseudo-OOS，未产生新的独立留出。', '',
              '全网格的同时区块区间仍较宽，波动最佳值在第二轮的校正区间覆盖零。第三轮窄参数族区间较好，只校正该轮内比较，不能抵消跨轮搜索。不能据此宣布统计显著或可交易Alpha。', '',
              '改进以MSE下降衡量，不是收益率、胜率或上涨概率。波动大只意味着未来振幅可能大，不能机械导出买卖。检验不包含交易策略、成本或仓位。', '',
              '## 固定候选与交付','',
              'shadow_candidate.json 已保存当前真实创建时间、输入哈希、固定候选和当前风险预测；没有补写过去预测。主页面维持环境监测定位。后续真实新样本按成熟标签检验，先比较集合、λ=.97、过去20/60日波动及长期均值；不因几周误差再次改参数。至少积累26个非重叠20交易日标签后，再进行预先约定的稳定性复核；这只是最低样本门槛，不等于统计充分。', '',
              '完整表：黄金调参账本.xlsx；所有训练标签截止日、每周模型选择、全部预测在各轮 forecasts 文件；manifest、代码与输入快照可核对。新增研究测试通过，不影响实时评分或后端服务。', '',
              '复算（项目根目录）：', '```sh',
              'OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/gold_tuning.py --inputs outputs/gold-implementation-2026-10-20/current-inputs.json --output research/output/gold_tuning_2026-10-20',
              'OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/gold_tuning.py --inputs outputs/gold-implementation-2026-10-20/current-inputs.json --output research/output/gold_tuning_2026-10-20/round2 --round2',
              'backend/.venv/bin/python backend/gold_tuning_followup.py',
              'backend/.venv/bin/python backend/gold_tuning_report.py', '```', '']
    (OUT/'调参结论.md').write_text('\n'.join(lines))
    print('Evaluation rows:',len(flat),'configuration-target combinations:',count)


if __name__=='__main__':
    main()
