"""Exploratory 1/3/5/10-observation bond forecasts, with explicit execution lag.

Run: OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/bond_short_research.py
All parameter selection uses 2014-2019. Previously viewed history is NOT fresh OOS.
"""
from __future__ import annotations
import hashlib
import json
from datetime import date
from pathlib import Path
import numpy as np
from bond_research import load, series
from bond_tuning import PARTITIONS, ridge_predictions, rolling_mean, summary, trend_prior

ROOT = Path('research/output/bonds_short_horizon_2026-10-21')
SOURCE = Path('research/output/bonds_tuning')
PROTOCOL = Path('research/bonds_short_horizon_protocol_2026-10-21.md')
SEED = 20261021


def change(values, k, is_yield=False):
    out = np.full(len(values), np.nan)
    out[k:] = ((values[k:]-values[:-k])*100 if is_yield else
               (values[k:]/values[:-k]-1)*10000)
    return out


def rolling_std(values, window=20):
    out=np.full(len(values),np.nan)
    for t in range(window,len(values)):
        out[t]=np.std(values[t-window+1:t+1])
    return out


def short_labels(values,h,lag,is_yield=False):
    if h<1 or lag not in (0,1):raise ValueError('positive horizon and lag 0/1 required')
    out=np.full(len(values),np.nan);n=len(values)-h-lag
    if n<=0:return out
    start, end=values[lag:lag+n],values[lag+h:lag+h+n]
    out[:n]=(end-start)*100 if is_yield else (end/start-1)*10000
    return out


def partition(dates,h,lag,part):
    start,end=PARTITIONS[part];exits=np.full(len(dates),'9999-99-99',dtype='<U10')
    exits[:-h-lag]=dates[h+lag:]
    return (dates>=start)&(exits<=end)


def feature_groups(panel,own,is_yield=False):
    ten=panel['yield10Y'];curve=panel['curve'];one=ten-curve[:,1];thirty=ten+curve[:,3]
    y_mom=np.column_stack([change(ten,k,True) for k in (1,3,5,10,20)])
    y_vol=rolling_std(change(ten,1,True))
    short=np.column_stack([y_mom,y_vol])
    cross=np.column_stack([change(panel[name],k) for name in ('3-5Y','7-10Y') for k in (1,3,5)])
    curves=np.column_stack([curve,change(one,1,True),change(one,5,True),
                            change(thirty,1,True),change(thirty,5,True),cross])
    own_mom=np.column_stack([change(own,k,is_yield) for k in (1,3,5,10,20)])
    days=[date.fromisoformat(str(d)) for d in panel['dates']]
    calendar=np.array([[float(d.weekday()==k) for k in range(1,5)]+[float(d.day>=25)] for d in days])
    combined=np.column_stack([short,curves,own_mom,rolling_std(own_mom[:,0]),calendar])
    nonlinear=np.column_stack([combined,y_mom[:,0]*y_mom[:,2],y_mom[:,0]*y_vol,
                               curve[:,1]*y_mom[:,0]])
    return {'short_momentum':short,'curve_market':curves,'combined':combined,'nonlinear':nonlinear}


def candidate_predictions(features,y,own,h,lag,is_yield,short_values=None,carry=None):
    # Helper's end is t-purge; last admitted i is t-h-lag, ending at t.
    purge=h+lag-1
    base=rolling_mean(y,purge,756)
    ps={'zero':np.zeros(len(y)),'mean_756':base,'mean_252':rolling_mean(y,purge,252)}
    for window in (1,3,5,10,20):
        trend=change(own,window,is_yield)
        if short_values is not None:trend-=change(short_values,window)
        for weight in (.25,.5,1.):
            for sign,name in ((1,'trend'),(-1,'reversal')):
                ps[f'{name}_{window}_{weight}']=trend_prior(base,trend*h/window,weight,sign)
    for group,x in features.items():
        for window in (252,756,1260):
            for alpha in (10.,100.,1000.):
                pred=ridge_predictions(x,y,purge,window,alpha)
                for weight in (.25,.5,1.):
                    ps[f'ridge_{group}_{window}_{alpha}_{weight}']=(1-weight)*base+weight*pred
    if carry is not None:
        ps['carry']=carry;ps['carry_half']=.5*(carry+base)
    return ps


def choose(dates,y,ps,h,lag):
    base=ps['mean_756'];rows=[]
    masks={part:partition(dates,h,lag,part)&np.isfinite(y)&np.isfinite(base)
           for part in ('validation1','validation2')}
    for name,pred in ps.items():
        stats={part:summary(y,pred,base,mask) for part,mask in masks.items()}
        if any(v['n']!=int(masks[part].sum()) for part,v in stats.items()):continue
        rows.append({'model':name,'loss_ratio':1-float(np.mean([v['r2_mean'] for v in stats.values()])),**stats})
    ranked=sorted(rows,key=lambda z:(z['loss_ratio'],z['model']))
    members=[r['model'] for r in ranked if r['model'].startswith('ridge_')][:5]
    ps['ensemble_top5']=np.mean([ps[name] for name in members],axis=0)
    stats={part:summary(y,ps['ensemble_top5'],base,mask) for part,mask in masks.items()}
    rows.append({'model':'ensemble_top5','loss_ratio':1-float(np.mean([v['r2_mean'] for v in stats.values()])),**stats})
    best=min(rows,key=lambda z:(z['loss_ratio'],z['model']))
    return best['model'],rows,members


def inference(y,pred,base,reps=2000):
    delta=(y-base)**2-(y-pred)**2;rng=np.random.default_rng(SEED);result={}
    for block in (5,20,60):
        b=min(block,len(y));sims=[]
        for _ in range(reps):
            starts=rng.integers(len(delta),size=int(np.ceil(len(delta)/b)))
            idx=((starts[:,None]+np.arange(b))%len(delta)).ravel()[:len(delta)]
            sims.append(float(delta[idx].mean()))
        result[str(block)]={'mean':float(delta.mean()),'ci95':np.quantile(sims,[.025,.975]).tolist(),
                            'p_one_sided':(1+sum(v>=2*delta.mean() for v in sims))/(reps+1)}
    return result


def economics(long,short,pred,mask,h):
    """All-in switch cost per unit allocation change; short bonds replace cash."""
    rows=[]
    for phase in range(h):
        origins=np.flatnonzero(mask&np.isfinite(long)&np.isfinite(short)&np.isfinite(pred)&
                               (np.arange(len(long))%h==phase))
        if not len(origins):continue
        for model,w in (('long_hold',np.ones(len(origins))),('short_hold',np.zeros(len(origins))),
                        ('selected',(pred[origins]>0).astype(float))):
            turnover=np.abs(np.diff(np.r_[0.,w,0.]))
            for cost in (0.,.5,1.,2.,5.):
                ret=(w*long[origins]+(1-w)*short[origins]-cost*turnover[:-1])/10000
                ret[-1]-=cost*turnover[-1]/10000
                nav=np.r_[1.,np.cumprod(1+ret)]
                rows.append({'phase':phase,'model':model,'cost_bp':cost,'periods':len(origins),
                             'return_pct':float((nav[-1]-1)*100),'turnover':float(turnover.sum()),
                             'max_drawdown_pct':float(np.min(nav/np.maximum.accumulate(nav)-1)*100)})
    return rows


def run(root=ROOT,source=SOURCE):
    root.mkdir(parents=True,exist_ok=True)
    data=np.load(source/'data_panel.npz');panel={k:data[k] for k in data.files};dates=panel['dates']
    input_paths=[source/'data_panel.npz']
    def index(name,metric):
        path=source/'inputs'/f'index_{name}_{metric}.json';input_paths.append(path)
        mapping=series(load(path)['series']);return np.array([mapping.get(str(d),np.nan) for d in dates])
    short=index('short','财富');short_yield=index('short','平均市值法到期收益率')
    yields={target:index(target,'平均市值法到期收益率') for target in ('3-5Y','7-10Y')}
    protocol=PROTOCOL.read_bytes();(root/'protocol.md').write_bytes(protocol)
    manifest={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in input_paths+[PROTOCOL,Path(__file__)]}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    jobs=[];rankings=[];choices=[]
    for target in ('yield10Y','3-5Y_gross','7-10Y_gross','3-5Y_excess_short','7-10Y_excess_short'):
        is_yield=target=='yield10Y';excess=target.endswith('excess_short');name=target.split('_')[0]
        own=panel[name];features=feature_groups(panel,own,is_yield)
        for lag in (0,1):
            for h in (1,3,5,10):
                key=f'{target}_h{h}_lag{lag}';print('fit',key,flush=True)
                y=short_labels(own,h,lag,is_yield)
                if excess:y-=short_labels(short,h,lag)
                carry=None if is_yield else (yields[name]-(short_yield if excess else 0))*100*h/252
                ps=candidate_predictions(features,y,own,h,lag,is_yield,short if excess else None,carry)
                selected,rows,members=choose(dates,y,ps,h,lag)
                choice={'task':key,'target':target,'horizon':h,'lag':lag,'selected':selected,'count':len(rows),
                        'validation_loss_ratio':next(r['loss_ratio'] for r in rows if r['model']==selected),
                        'ensemble_members':members}
                choices.append(choice);rankings.extend([{'task':key,**r} for r in rows])
                np.savez_compressed(root/f'predictions_{key}.npz',actual=y,**ps)
                jobs.append((choice,y,ps))
    (root/'selection_frozen.json').write_text(json.dumps({'choices':choices,'manifest':manifest,
        'warning':'Retrospective exploration on already-viewed history; no new independent holdout'},indent=2))
    (root/'validation_rankings.json').write_text(json.dumps(rankings,indent=2))
    print('CHOICES FROZEN; EVALUATING',flush=True)
    results=[]
    for choice,y,ps in jobs:
        key=choice['task'];h=choice['horizon'];lag=choice['lag'];selected=choice['selected']
        base=ps['mean_756'];pred=ps[selected]
        r={**choice,'partitions':{},'years':{},'families':{},'bootstrap':{}}
        for part in PARTITIONS:
            mask=partition(dates,h,lag,part)
            r['partitions'][part]={name:summary(y,ps[name],base,mask) for name in dict.fromkeys(('zero','mean_756',selected))}
        for part in ('audit','extension_used'):
            mask=partition(dates,h,lag,part)&np.isfinite(y)&np.isfinite(pred)&np.isfinite(base)
            r['bootstrap'][part]=inference(y[mask],pred[mask],base[mask])
            r[f'{part}_nonoverlap_r2']=[summary(y,pred,base,mask&(np.arange(len(y))%h==phase))['r2_mean'] for phase in range(h)]
            if lag==1 and choice['target']!='yield10Y':
                name=choice['target'].split('_')[0]
                edge=pred if choice['target'].endswith('excess_short') else pred-short_yield*100*h/252
                r[f'economics_{part}']=economics(short_labels(panel[name],h,1),short_labels(short,h,1),edge,mask,h)
        for year in range(2020,2027):
            mask=partition(dates,h,lag,'audit' if year<2023 else 'extension_used')&np.char.startswith(dates,str(year))
            r['years'][str(year)]=summary(y,pred,base,mask)
        taskrows=[row for row in rankings if row['task']==key]
        for family in ('trend','reversal','ridge_short_momentum','ridge_curve_market','ridge_combined','ridge_nonlinear'):
            eligible=[row for row in taskrows if row['model'].startswith(family+'_')]
            best=min(eligible,key=lambda z:z['loss_ratio']);p=ps[best['model']]
            r['families'][family]={'selected':best['model'],'validation_loss_ratio':best['loss_ratio'],
                **{part:summary(y,p,base,partition(dates,h,lag,part)) for part in ('audit','extension_used')}}
        if selected.startswith('ridge_'):
            group=selected.removeprefix('ridge_').rsplit('_',3)[0]
            neighbors=[p for name,p in ps.items() if name.startswith(f'ridge_{group}_')]
            vals=[summary(y,p,base,partition(dates,h,lag,'audit'))['r2_mean'] for p in neighbors]
            r['neighborhood']={'n':len(vals),'positive_fraction':float(np.mean(np.array(vals)>0)),
                               'median_r2':float(np.median(vals))}
        a=r['partitions']['audit'][selected];e=r['partitions']['extension_used'][selected]
        r['candidate_prior']=a['r2_mean']>0 and e['r2_mean']>0 and sum((r['years'][str(yr)]['r2_mean'] or 0)>0 for yr in (2020,2021,2022))>=2
        results.append(r)
    # Claim must survive both historical segments and serial-dependence choices.
    pv=np.array([max(z['p_one_sided'] for b in r['bootstrap'].values() for z in b.values()) for r in results])
    order=np.argsort(pv);q=np.minimum.accumulate((pv[order]*len(pv)/np.arange(1,len(pv)+1))[::-1])[::-1]
    for i,value in zip(order,q):results[i]['bh_q']=float(min(1,value))
    for r in results:r['strong_evidence']=r['candidate_prior'] and r['bh_q']<.05 and all(z['ci95'][0]>0 for part in r['bootstrap'].values() for z in part.values())
    report={'status':'exploratory_short_horizon','source_panel_dates':[str(dates[0]),str(dates[-1])],
            'n_dates':len(dates),'total_comparisons':sum(c['count'] for c in choices),
            'protocol_sha256':hashlib.sha256(protocol).hexdigest(),'results':results}
    (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for r in results:
        print(r['task'],r['selected'],round(r['partitions']['audit'][r['selected']]['r2_mean'],4),
              round(r['partitions']['extension_used'][r['selected']]['r2_mean'],4),r['candidate_prior'],round(r['bh_q'],3),flush=True)
    return report

if __name__=='__main__':run()
