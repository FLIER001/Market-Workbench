"""Human-readable candidate, exact parameters and complete directional trial ledger."""
import json
from pathlib import Path
from gold_tuning import dump
from gold_direction_finalize import PRIMARY

ROOT=Path(__file__).parents[1]
OUT=ROOT/'research/output/gold_direction_2026-10-20'


def main():
    confirmation=json.loads((OUT/'confirmation/results.json').read_text())
    current=json.loads((OUT/'frozen_candidate.json').read_text())['candidate']
    rows=[]; references={}
    for number,folder in enumerate((OUT,OUT/'round2',OUT/'round3',OUT/'confirmation'),1):
        report=json.loads((folder/'results.json').read_text())
        panels=report.get('panels') or {'long':report['horizons']}
        for panel,hs in panels.items():
            for h,result in hs.items():
                for name,m in result['metrics'].items():
                    rows.append([number,panel,int(h),name,m['n'],m['balanced_accuracy'],m['auc'],
                        m['accuracy'],m['always_up_accuracy'],m['brier'],result['metrics']['base']['brier'],
                        m['brier_skill'],*m['fold_balanced_accuracy'],m['tail52_balanced_accuracy'],
                        m['tail52_auc'],m['tail52_brier_skill']])
                    if number==4 and name==PRIMARY:references[h]=len(rows)+5
    main=confirmation['horizons']['20']; m=main['metrics'][PRIMARY]
    neighbors=[(k,v) for k,v in main['metrics'].items() if k.startswith('trend')]
    dump(OUT/'workbook-data.json',{'rows':rows,'references':references,'current':current,
        'primary_metrics':m,'bands':main['primary_bands'],
        'neighbors':[[k,v['balanced_accuracy'],v['auc'],v['brier_skill'],v['tail52_balanced_accuracy']]
                     for k,v in neighbors]})
    ci=main['primary_interval']; bands=main['primary_bands']; tail=main['primary_tail52_bands']
    lines=['# 黄金20日方向弱先验：方案与固定参数','',
        '本轮可以保留一个辅助方案：60日趋势为主，5日反转为辅，再向中性大幅收缩。它提供的是弱方向倾向，不承诺精确收益或高胜率。', '',
        '## 固定参数','',
        '|参数|取值|','|---|---|','|预测期限|20个交易日|','|中期趋势|60个交易日累计对数收益|',
        '|短期反转|5个交易日累计对数收益的反向|','|波动尺度|过去20个日收益样本标准差|',
        '|趋势/反转权重|90% / 10%|','|收缩系数|25%，其余75%回到中性50分|',
        '|分档|≥52偏多，≤48偏空，其他中性|','|更新及历史评价|日价格可更新；历史每周末决策，下一PM入场|', '',
        '公式中σ20为过去20个日对数收益的样本标准差，Φ为标准正态分布函数：',
        '```text','z趋势 = ln(Pt / Pt-60) × √20 / (60 × σ20)',
        'z反转 = -ln(Pt / Pt-5) × √20 / (5 × σ20)',
        'q = 0.90 × Φ(z趋势) + 0.10 × Φ(z反转)',
        '方向分 = 100 × [0.50 + 0.25 × (q - 0.50)]','```', '',
        '方向分理论范围37.5–62.5。它是保守模型倾向分，不是经验证的上涨概率。没有“强多/强空”或分数越极端就越可信的规则。', '',
        '## 放宽标准后，为什么保留它','',
        f'共完成四轮方向研究，完整账本{len(rows):,}行，包含所有候选、重复复测及基准。所有训练标签仍严格早于决策时间，未为提高指标使用未来数据。主期限20日；5/60日全部报告。', '',
        '|指标|主方案|比较口径|','|---|---:|---|',
        f"|全段平衡准确率|{m['balanced_accuracy']:.1%}|上涨与下跌召回率等权；无区分力为50%|",
        f"|全段普通命中率|{m['accuracy']:.1%}|相同日期一律偏多为{m['always_up_accuracy']:.1%}|",
        f"|全段AUC|{m['auc']:.3f}|无排序区分力为0.5|",
        f"|Brier误差改善|{m['brier_skill']:.1%}|对扩展历史上涨比例|",
        f"|对近52/104周比例的误差改善|{m['skill_vs_prior52']:.1%} / {m['skill_vs_prior104']:.1%}|同日期|",
        f"|对恒定50%模型的误差改善|{main['brier_skill_vs_half']:.1%}|区分信号与单纯回归中性的效果|",
        f"|最近52周平衡准确率 / AUC|{m['tail52_balanced_accuracy']:.1%} / {m['tail52_auc']:.3f}|历史最近段，非独立新留出|", '',
        '全段2020-05-15至2026-09-18，共332个周预测。三等分时段平衡准确率为'+
        ' / '.join(f'{v:.1%}' for v in m['fold_balanced_accuracy'])+'。普通命中率只比一律偏多高约0.6个百分点，实际价值偏弱，不能用平衡准确率代替交易胜率。', '',
        '局部检查窗口50/60/70日、趋势权重85/90/95%、收缩15/25/35%，27组均为正Brier改善，平衡准确率均高于50%，范围'+
        f"{min(v['balanced_accuracy'] for _,v in neighbors):.1%}–{max(v['balanced_accuracy'] for _,v in neighbors):.1%}。主方案在局部检查前已指定，没有再用局部最高值替换。", '',
        '## 怎么用，哪里有局限','',
        '只把偏多/偏空当辅助倾向；中性时不作方向判断。偏空表示减少原有偏多先验，不自动等于做空指令。三参数波动风险预测仍独立存在，不与方向分混成总分。', '',
        '|方向分档|周样本|实际上涨率|该方向命中率|平均20日对数收益|','|---|---:|---:|---:|---:|']
    for label in ('偏多','中性','偏空'):
        b=bands[label]
        accuracy=f"{b['accuracy']:.1%}" if b['accuracy'] is not None else '—'
        lines.append(f"|{label}|{b['n']}|{b['positive_share']:.1%}|{accuracy}|{b['mean_log_return']:.2%}|")
    lines += ['',f"52/48分档覆盖{bands['combined']['coverage']:.1%}的周时点；非中性样本{bands['combined']['n']}个，命中{bands['combined']['accuracy']:.1%}，相同日期一律偏多{bands['combined']['always_up_same_dates']:.1%}。最近52周非中性样本{tail['combined']['n']}个，命中{tail['combined']['accuracy']:.1%}，但该近期段早已查看。", '',
        '偏多、偏空的平均收益差仅为条件样本描述，存在标签交叠，没有仓位和交易成本，不是策略收益。中性组上涨率高于偏多组，提示模型分档并未实现严格单调校准；不能把高分解释为更高置信度。', '',
        '按实际入场/到期日分离后的相位有74–75个样本；平衡准确率约50.0%–63.6%，Brier相位有正有负。2021/2024年平衡准确率低于50%，2025年Brier较差，完整逐年结果保留。', '',
        f"单个事后选定模型的移动区块95%区间：平衡准确率{ci['balanced_accuracy_ci95'][0]:.1%}–{ci['balanced_accuracy_ci95'][1]:.1%}，Brier改善{ci['brier_skill_ci95'][0]:.1%}–{ci['brier_skill_ci95'][1]:.1%}。区间覆盖无效值，且未校正全部自适应搜索。按用户接受的辅助先验标准保留，而非宣布统计验证成功。", '',
        '输入为最新修订公共数据，历史发布日采用保守代理。本轮研究者已查看历史，并根据前轮结果增加候选；没有独立前瞻证据。最近一年更好不能推断未来维持该水平。', '',
        '## 当前快照与交付','',
        f"真实创建时间{current['created_at']}，价格观测截至{current['observed_at']}。固定模型当前方向分{current['score']:.1f}，落在“{current['direction']}”区间；不因为希望有信号而改阈值。", '',
        '纯计算实现：`backend/gold_direction.py`。参数及当前首次记录：`frozen_candidate.json`；局部完整检验：`confirmation/results.json`。页面和交易接口均未修改，当前仅交付可以复现的研究候选。', '',
        '复算（项目根目录）：','```sh',
        'OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/gold_direction_research.py --inputs research/output/gold_tuning_2026-10-20/inputs.json --output research/output/gold_direction_2026-10-20',
        'OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/gold_direction_research.py --inputs research/output/gold_tuning_2026-10-20/inputs.json --output research/output/gold_direction_2026-10-20/round2 --fast',
        'OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/gold_direction_research.py --inputs research/output/gold_tuning_2026-10-20/inputs.json --output research/output/gold_direction_2026-10-20/round3 --neighborhood',
        'backend/.venv/bin/python backend/gold_direction_finalize.py',
        'backend/.venv/bin/python backend/gold_direction_report.py','```','']
    (OUT/'方案与参数.md').write_text('\n'.join(lines))
    print('Complete evaluation rows',len(rows),'current score',current['score'])


if __name__=='__main__':main()
