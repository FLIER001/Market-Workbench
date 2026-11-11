"""Frozen candidate specifications + rolling mature-label fits and exploratory scores.

Short/medium/long refer to bond maturity. Models retain their separate forecast
windows and retrospective evidence. Required live sources never fall back to
unlabelled proxies. The original manual framework/AI scores are not modified.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import numpy as np
from bond_math import finite
from bond_research import series
from bond_tuning import labels_for
from bond_short_tenor import features_for

DATA=Path(__file__).with_name('data')
TZ=timezone(timedelta(hours=8))
INDEX_JOBS={'short_index':('财富','1年以下'),'3-5Y':('财富','3-5年'),
            '7-10Y':('财富','7-10年'),'short_ytm':('平均市值法到期收益率','1年以下'),
            'short_duration':('平均市值法久期','1年以下')}


@lru_cache(maxsize=1)
def artifacts():
    config=json.loads((DATA/'bond_prior_models.json').read_text())
    raw=(DATA/'bond_prior_history.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=config['history_sha256']:
        raise ValueError('冻结训练历史校验失败')
    history=json.loads(raw)
    return config,{key:dict(zip(history['dates'],values)) for key,values in history['series'].items()}


def stale_date(observed,today):
    day=date.fromisoformat(observed)
    if day>today or (today-day).days>7:return True
    return sum((day+timedelta(days=i)).weekday()<5 for i in range(1,(today-day).days))>0


def fit_current(x,y,h,window,alpha,shrink):
    """At last origin t, i+h+1<=t; identical scaling/penalty to research fits."""
    t=len(y)-1;end=t-h;start=max(0,end-window)
    idx=np.arange(start,end)
    idx=idx[np.isfinite(y[idx])&np.isfinite(x[idx]).all(axis=1)]
    if len(idx)<window or not np.isfinite(x[t]).all():
        raise ValueError('冻结参数要求的完整训练窗口或当期特征不足')
    train=x[idx];target=y[idx];center=train.mean(axis=0);scale=train.std(axis=0)
    scale[scale<1e-8]=1
    z=(train-center)/scale
    beta=np.linalg.solve(z.T@z+alpha*np.eye(x.shape[1]),z.T@(target-target.mean()))
    raw=float(target.mean()+((x[t]-center)/scale)@beta)
    mean_idx=np.flatnonzero(np.isfinite(y[:end]))[-756:]
    if len(mean_idx)<756:raise ValueError('历史均值基准样本不足')
    baseline=float(y[mean_idx].mean())
    return baseline+shrink*(raw-baseline),baseline,{'n':len(idx),'last_origin_index':int(idx[-1]),
        'last_label_end_index':int(idx[-1]+h+1),'window':window,'alpha':alpha,'shrink':shrink}


SCORE_VERSION = 'relative-return-v1'


def relative_score(prediction, baseline, volatility):
    """Display scale, not calibrated probability; 50 means no baseline advantage."""
    if not all(finite(v) for v in (prediction, baseline, volatility)) or volatility <= 1e-8:
        return None
    return round(float(50 + 50 * np.tanh((prediction - baseline) / volatility)), 2)


@lru_cache(maxsize=8)
def score_history(payload):
    """Re-fit each historical origin with mature labels only, latest 252 dates.

    Source vintages are not available: this is a retrospective reconstruction
    with researched hyperparameters, never an archive of published forecasts.
    Cache includes all input values and members so revisions invalidate it.
    """
    data=json.loads(payload)
    dates=data['dates'];own=np.asarray(data['own']);h=data['horizon']
    y=labels_for(own,h,False)
    groups={key:np.asarray(value) for key,value in data['groups'].items()}
    history=[]
    for t in range(max(0,len(dates)-252),len(dates)):
        prediction=[];baseline=[]
        try:
            for member in data['members']:
                group,window,alpha,shrink=member.removeprefix('ridge_').rsplit('_',3)
                p,b,_=fit_current(groups[group][:t+1],y[:t+1],h,int(window),float(alpha),float(shrink))
                prediction.append(p);baseline.append(b)
            mature=y[:max(0,t-h)];mature=mature[np.isfinite(mature)][-756:]
            vol=float(np.std(mature,ddof=1)) if len(mature)>=756 else None
            p=float(np.mean(prediction));b=float(np.mean(baseline))
            score=relative_score(p,b,vol)
        except (ValueError,np.linalg.LinAlgError):
            p=b=vol=score=None
        history.append({'date':dates[t],'score':score,'index':float(own[t]),
                        'forecast_bp':p,'baseline_bp':b,'volatility_bp':vol})
    return history


def forecast(curve,aux,now=None):
    now=now or datetime.now(TZ);today=now.date()
    try:config,frozen=artifacts()
    except (OSError,ValueError,KeyError) as exc:
        return {'status':'unavailable','rows':[],'reason':str(exc),'generated_at':now.isoformat()}
    live={key:series(points) for key,points in (curve.get('yields') or {}).items()}
    blocks=aux.get('series') or {}
    for key,block in blocks.items():live[key]=series(block.get('points'))
    live={k:{d:v for d,v in m.items() if d<=today.isoformat()} for k,m in live.items()}
    merged={k:{**values,**live.get(k,{})} for k,values in frozen.items()}
    rows=[];used={}
    for spec in config['models']:
        row={**spec,'model_version':config['version'],'status':'missing','forecast_bp':None,
             'historical_estimate_bp':None,'baseline_bp':None,'observed_at':None,
             'unit':'yield_change_bp' if spec['target']=='gov3M' else 'total_return_bp',
             'reason':None,'training':[],'source_dates':{},'score':None,'score_history':[],
             'score_version':SCORE_VERSION,'history_kind':'retrospective'}
        required=['3月','6月','1年','10年'] if spec['family']=='short_curve' else ['1年','5年','10年','30年']
        if spec['target']!='gov3M':required+=[spec['target']]
        if spec['target']=='short_index':required+=['short_ytm','short_duration']
        try:
            missing=[key for key in required if not live.get(key)]
            failed=[key for key in required if key in blocks and blocks[key].get('error')]
            if missing:raise ValueError('必要实时来源缺失：'+', '.join(missing))
            if failed:raise ValueError('必要来源更新失败：'+', '.join(failed))
            if curve.get('refresh_error') or curve.get('cache_state')=='error':
                raise ValueError('国债曲线更新失败，暂停新估计')
            row['source_dates']={key:max(live[key]) for key in required}
            # Choose an actually common date; newer observations in another series
            # never leak into this origin's features or training set.
            common=set.intersection(*(set(merged[key]) for key in required))
            ceiling=min(row['source_dates'].values());dates=sorted(d for d in common if d<=ceiling)
            if not dates:raise ValueError('必要来源没有共同观测日')
            observed=dates[-1];row['observed_at']=observed
            if any((date.fromisoformat(v)-date.fromisoformat(observed)).days>7 for v in row['source_dates'].values()):
                raise ValueError('来源日期差异超过7天，暂停新估计')
            values={key:np.array([merged[key][d] for d in dates]) for key in required}
            h=spec['horizon'];own=values['3月'] if spec['target']=='gov3M' else values[spec['target']]
            y=labels_for(own,h,spec['target']=='gov3M')
            if spec['family']=='short_curve':
                yields={k:values[v] for k,v in [('gov3M','3月'),('gov6M','6月'),('gov1Y','1年'),('gov10Y','10年')]}
                groups=features_for(yields,{},own,spec['target']=='gov3M',values.get('short_ytm'),values.get('short_duration'))
            else:
                ten=values['10年']
                momentum=np.column_stack([np.r_[np.full(k,np.nan),ten[k:]-ten[:-k]] for k in (5,20,60,120)])
                groups={'momentum':momentum}
            predictions=[];baselines=[]
            for member in spec['members']:
                group,window,alpha,shrink=member.removeprefix('ridge_').rsplit('_',3)
                p,b,training=fit_current(groups[group],y,h,int(window),float(alpha),float(shrink))
                training.update({'member':member,'last_origin':dates[training.pop('last_origin_index')],
                                 'last_label_end':dates[training.pop('last_label_end_index')]})
                row['training'].append(training);predictions.append(p);baselines.append(b)
            estimate=float(np.mean(predictions));baseline=float(np.mean(baselines))
            if not finite(estimate) or not finite(baseline):raise ValueError('估计值无效')
            row.update(status='ready',forecast_bp=round(estimate,6),baseline_bp=round(baseline,6))
            if spec['target']!='gov3M':
                selected_groups={member.removeprefix('ridge_').rsplit('_',3)[0] for member in spec['members']}
                history=score_history(json.dumps({'dates':dates,'own':own.tolist(),'horizon':h,
                    'members':spec['members'],'groups':{k:groups[k].tolist() for k in sorted(selected_groups)}}))
                row['score_history']=history
                row['score']=history[-1]['score'] if history else None
            if stale_date(observed,today):
                row.update(status='stale',forecast_bp=None,score=None,historical_estimate_bp=round(estimate,6),
                           reason='共同观测日滞后；暂停当前估计，历史估计仅供核对。')
            # The archive references a hash of the immutable training backbone,
            # and stores live overlay sources separately (in bonds.py).
            used[spec['id']]={'date':observed,'series':{k:[[d,merged[k][d]] for d in dates[-1500:]] for k in required}}
        except (ValueError,KeyError,np.linalg.LinAlgError) as exc:
            row.update(status='missing',forecast_bp=None,reason=str(exc))
        rows.append(row)
    fingerprint=hashlib.sha256(json.dumps({'config':config,'score_version':SCORE_VERSION,'inputs':used,'health':{
        k:[v.get('error'),v.get('fetched_at')] for k,v in blocks.items()}},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
    return {'model_version':config['version'],'status':'experimental','independent_oos':False,
        'generated_at':now.isoformat(),'input_fingerprint':fingerprint,'rows':rows,
        'history_cutoff':config['history_cutoff'],'history_sha256':config['history_sha256'],
        'notes':['债券期限与预测窗口分开；窗口单位为共同观测日。',
                 't收盘信息，t+1观测日起点、t+h+1终点；并非下一开盘成交预测。',
                 '固定参数、滚动重估；历史已用于研究，尚无独立前瞻验证或稳定择时Alpha。',
                 '得分=50+50×tanh((预测收益−756个已到期样本均值)/同批收益标准差)，不是上涨概率；55/45仅为展示分界。',
                 '历史为当前数据版本逐日回溯重算，参数经过历史挑选、来源可能修订，不代表当时发布的得分。']}
