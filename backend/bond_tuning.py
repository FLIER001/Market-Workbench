"""Offline bond forecast tuning. Protocol: research/bonds_tuning_protocol_2026-10-20.md.

No production signal. All hyperparameter selection ends before the audit period.
python backend/bond_tuning.py [--output-dir research/output/bonds_tuning]
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from bond_research import load, series

SEED = 20261020
PARTITIONS = {'validation1': ('2014-01-01', '2016-12-31'),
              'validation2': ('2017-01-01', '2019-12-31'),
              'audit': ('2020-01-01', '2022-12-31'),
              'extension_used': ('2023-01-01', '2099-12-31')}


def panel(curve, indices):
    maps = {k: series(curve['yields'].get(k)) for k in ('1年','5年','10年','30年')}
    dates = sorted(set.intersection(*(set(v) for v in maps.values())))
    levels = np.array([[maps[k][d] for k in maps] for d in dates])
    one, five, ten, thirty = levels.T
    lag = lambda a, k: a[k:] - a[:-k]
    curve_x = np.column_stack([ten, ten-one, 2*five-one-ten, thirty-ten])[120:]
    momentum = np.column_stack([lag(ten,k)[120-k:] for k in (5,20,60,120)])
    # Fixed products introduce no full-sample estimated thresholds or normalization.
    nonlinear = np.column_stack([curve_x, momentum, curve_x[:,0]*curve_x[:,1],
                                 curve_x[:,1]*momentum[:,1], curve_x[:,0]*momentum[:,2]])
    targets = {'yield10Y': ten[120:]}
    for name, points in indices['series'].items():
        m = series(points)
        targets[name] = np.array([m.get(d,np.nan) for d in dates[120:]])
    return np.array(dates[120:]), {'curve':curve_x, 'momentum':momentum,
            'combined':np.column_stack([curve_x,momentum]), 'nonlinear':nonlinear}, targets


def labels_for(values, horizon, is_yield):
    y = np.full(len(values), np.nan)
    y[:-horizon-1] = ((values[horizon+1:]-values[1:-horizon])*100 if is_yield else
                     (values[horizon+1:]/values[1:-horizon]-1)*10000)
    return y


def rolling_mean(y, horizon, window):
    n = len(y)
    end = np.maximum(0, np.arange(n)-horizon)  # last origin is t-h-1, label ends t
    start = np.maximum(0,end-window)
    count = np.r_[0,np.cumsum(np.isfinite(y))]
    sums = np.r_[0,np.cumsum(np.nan_to_num(y))]
    size = count[end]-count[start]
    return np.divide(sums[end]-sums[start],size,out=np.full(n,np.nan),where=size>=60)


def ridge_predictions(x, y, horizon, window, alpha):
    """Vectorized daily fits, standardized only by each available training window."""
    n,p = x.shape
    end = np.maximum(0,np.arange(n)-horizon)
    start = np.maximum(0,end-window)
    valid = np.isfinite(y) & np.isfinite(x).all(axis=1)
    count = np.r_[0,np.cumsum(valid)]
    size = count[end]-count[start]
    xx = np.where(valid[:,None],x,0.)
    yy = np.where(valid,y,0.)
    def sums(v):
        c = np.concatenate([np.zeros((1,*v.shape[1:])),np.cumsum(v,axis=0)])
        return c[end]-c[start]
    m = np.maximum(size,1)
    sx,sy = sums(xx),sums(yy)
    mu,ym = sx/m[:,None],sy/m
    cov = sums(xx[:,:,None]*xx[:,None,:])-sx[:,:,None]*sx[:,None,:]/m[:,None,None]
    var = np.maximum(np.diagonal(cov,axis1=1,axis2=2)/m[:,None],0)
    scale = np.sqrt(var);scale[scale<1e-8]=1
    gram = cov/(scale[:,:,None]*scale[:,None,:])
    gram += np.eye(p)[None,:,:]*alpha
    cross = (sums(xx*yy[:,None])-sx*ym[:,None])/scale
    beta = np.linalg.solve(gram,cross[:,:,None])[:,:,0]
    pred = ym+np.sum((x-mu)/scale*beta,axis=1)
    pred[size<252]=np.nan
    return pred


def candidates(features, y, values, h, is_yield):
    base = rolling_mean(y,h,756)
    out={'zero':np.zeros(len(y)), 'mean_756':base}
    for window in (60,120,252,504):
        out[f'mean_{window}']=rolling_mean(y,h,window)
    # Trailing price/yield trend is known at t, execution still delayed to t+1.
    for window in (20,60,120,252,504):
        change=np.full(len(y),np.nan)
        change[window:]=((values[window:]-values[:-window])*100 if is_yield else
                        (values[window:]/values[:-window]-1)*10000)
        for strength in (.25,.5,1.):
            for sign,name in ((1,'trend'),(-1,'reversal')):
                out[f'{name}_{window}_{strength}']=trend_prior(base,change*h/window,strength,sign)
    for group,x in features.items():
        for window in (252,504,756,1260):
            for alpha in (1.,10.,100.,1000.):
                pred=ridge_predictions(x,y,h,window,alpha)
                for shrink in (.25,.5,1.):
                    out[f'ridge_{group}_{window}_{alpha}_{shrink}']=base+shrink*(pred-base)
    return out


def trend_prior(base, trend, strength, sign):
    return (1-strength)*base+strength*sign*trend


def mask_partition(dates,h,part):
    start,end=PARTITIONS[part]
    exits=np.full(len(dates),'9999-99-99',dtype='<U10')
    exits[:-h-1]=dates[h+1:]
    return (dates>=start)&(exits<=end)


def summary(y,p,base,mask):
    keep=mask & np.isfinite(y)&np.isfinite(p)&np.isfinite(base)
    y,p,b=y[keep],p[keep],base[keep]
    if not len(y):return {'n':0,'r2_mean':None}
    loss=(y-p)**2; baseline=(y-b)**2
    return {'n':len(y),'rmse':float(np.sqrt(loss.mean())),
            'r2_mean':float(1-loss.sum()/baseline.sum()),
            'r2_zero':float(1-loss.sum()/(y*y).sum()),
            'direction':float(((p>0)==(y>0)).mean()),
            'mean_prediction':float(p.mean()),'mean_actual':float(y.mean())}


def bootstrap_improvement(y,p,b,h,reps=2000):
    delta=(y-b)**2-(y-p)**2
    rng=np.random.default_rng(SEED)
    result={}
    for block in (h,2*h):
        k=min(block,len(delta));sims=[]
        for _ in range(reps):
            starts=rng.integers(len(delta),size=int(np.ceil(len(delta)/k)))
            idx=((starts[:,None]+np.arange(k))%len(delta)).ravel()[:len(delta)]
            sims.append(float(delta[idx].mean()))
        # Centered bootstrap H0 E[loss improvement]=0; one-sided exploratory p.
        pvalue=(1+sum(v-delta.mean()>=delta.mean() for v in sims))/(reps+1)
        result[str(block)]={'mean':float(delta.mean()),'ci95':np.quantile(sims,[.025,.975]).tolist(),
                            'p_one_sided':pvalue}
    return result


def economic(dates, y, pred, mean, mask, h):
    rows=[]
    origins=np.flatnonzero(mask & np.isfinite(y)&np.isfinite(pred)&np.isfinite(mean))
    origins=origins[::h]  # Entry t+1, exit t+h+1; no overlapping holdings.
    for model,p in [('buy_hold',np.ones(len(y))),('mean',mean),('selected',pred)]:
        weights=(p[origins]>0).astype(float)
        turnover=np.abs(np.diff(np.r_[0.,weights,0.]))
        for cost in (0,2,5,10):
            returns=weights*y[origins]/10000-cost*turnover[:-1]/10000
            if len(returns):returns[-1]-=cost*turnover[-1]/10000
            nav=np.r_[1.,np.cumprod(1+returns)]
            rows.append({'model':model,'cost_bp':cost,'n_trades':len(origins),
                'total_return_pct':float((nav[-1]-1)*100),
                'max_drawdown_pct':float(np.min(nav/np.maximum.accumulate(nav)-1)*100),
                'turnover':float(turnover.sum())})
    return rows


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    inputs=output/'inputs'
    curve=load(inputs/'curve.json')
    index_path=Path('research/output/bonds_validation/inputs/bonds_segment_indices.json')
    dates,features,targets=panel(curve,load(index_path))
    protocol=Path('research/bonds_tuning_protocol_2026-10-20.md').read_bytes()
    manifests={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
               (inputs/'curve.json',index_path,Path('research/bonds_tuning_protocol_2026-10-20.md'))}
    np.savez_compressed(output/'data_panel.npz',dates=dates,**features,**targets)
    # All predictions can be computed ahead; selection below sees validation masks only.
    jobs=[];ranking=[];selections=[]
    for target,values in targets.items():
        for h in (20,60,120):
            key=f'{target}_{h}';print('fit',key,flush=True)
            y=labels_for(values,h,target=='yield10Y')
            ps=candidates(features,y,values,h,target=='yield10Y')
            base=ps['mean_756']
            common=np.isfinite(y)&np.isfinite(base)&np.isfinite(features['combined']).all(axis=1)
            # Common origins for every comparison; initial warmup precedes 2014.
            valid1=mask_partition(dates,h,'validation1')&common
            valid2=mask_partition(dates,h,'validation2')&common
            rows=[]
            for name,p in ps.items():
                a,b=summary(y,p,base,valid1),summary(y,p,base,valid2)
                if a['n']!=int(valid1.sum()) or b['n']!=int(valid2.sum()):continue
                score=1-(a['r2_mean']+b['r2_mean'])/2
                rows.append({'task':key,'model':name,'selection_loss_ratio':score,
                             'validation1':a,'validation2':b})
            ranked=sorted(rows,key=lambda r:(r['selection_loss_ratio'],r['model']))
            top=[r['model'] for r in ranked if r['model'].startswith('ridge_')][:5]
            ps['ensemble_top5']=np.mean([ps[name] for name in top],axis=0)
            p=ps['ensemble_top5']
            a,b=summary(y,p,base,valid1),summary(y,p,base,valid2)
            rows.append({'task':key,'model':'ensemble_top5','members':top,
                         'selection_loss_ratio':1-(a['r2_mean']+b['r2_mean'])/2,
                         'validation1':a,'validation2':b})
            ranked=sorted(rows,key=lambda r:(r['selection_loss_ratio'],r['model']))
            selected=ranked[0]['model']
            selections.append({'task':key,'selected':selected,'ensemble_members':top,
                               'validation_loss_ratio':ranked[0]['selection_loss_ratio'],
                               'candidate_count':len(rows)})
            ranking.extend(rows)
            np.savez_compressed(output/f'predictions_{key}.npz',actual=y,**ps)
            jobs.append((key,h,y,ps,selected))
    frozen={'input_sha256':manifests,'dates':[str(dates[0]),str(dates[-1])],
            'n_dates':len(dates),'choices':selections,
            'warning':'2023+ already examined; only 2020-2022 is newly evaluated audit block'}
    (output/'selection_frozen.json').write_text(json.dumps(frozen,indent=2))
    (output/'validation_rankings.json').write_text(json.dumps(ranking,indent=2))
    print('SELECTION FROZEN; revealing audit',flush=True)
    results=[]
    for key,h,y,ps,selected in jobs:
        base=ps['mean_756'];p=ps[selected]
        r={'task':key,'selected':selected,'partitions':{},'years':{},'families':{}}
        for part in PARTITIONS:
            mask=mask_partition(dates,h,part)
            r['partitions'][part]={name:summary(y,ps[name],base,mask) for name in
                                   dict.fromkeys(('zero','mean_756',selected))}
        mask=mask_partition(dates,h,'audit')&np.isfinite(y)&np.isfinite(p)&np.isfinite(base)
        r['audit_bootstrap']=bootstrap_improvement(y[mask],p[mask],base[mask],h)
        for year in range(2020,2027):
            m=mask_partition(dates,h,'audit' if year<2023 else 'extension_used')
            r['years'][str(year)]=summary(y,p,base,m&(np.char.startswith(dates,str(year))))
        r['audit_nonoverlap_r2']=[summary(y,p,base,mask&(np.arange(len(dates))%h==offset))['r2_mean']
                                   for offset in range(h)]
        # Validation-chosen winner per family, no audit-based selection.
        taskrows=[x for x in ranking if x['task']==key]
        for family in ('mean','trend','reversal','ridge_curve','ridge_momentum','ridge_combined','ridge_nonlinear','ensemble'):
            rs=[x for x in taskrows if x['model'].startswith(family)]
            if rs:
                best=min(rs,key=lambda z:z['selection_loss_ratio'])
                name=best['model']
                r['families'][family]={'selected':name,'validation_loss_ratio':best['selection_loss_ratio'],
                    'audit':summary(y,ps[name],base,mask_partition(dates,h,'audit')),
                    'extension':summary(y,ps[name],base,mask_partition(dates,h,'extension_used'))}
        if selected.startswith('ridge_'):
            # Same feature family; robustness across all predeclared parameter neighbors.
            prefix='_'.join(selected.split('_')[:2])+'_'
            neighbors=[name for name in ps if name.startswith(prefix)]
            rates=[summary(y,ps[name],base,mask_partition(dates,h,'audit'))['r2_mean'] for name in neighbors]
            r['neighborhood']={'n':len(rates),'positive_fraction':float(np.mean(np.array(rates)>0)),
                               'median_r2':float(np.median(rates))}
        if not key.startswith('yield'):
            r['economics_audit']=economic(dates,y,p,base,mask,h)
        results.append(r)
    # BH adjustment across the nine validation-selected task/horizon claims.
    pv=np.array([max(z['p_one_sided'] for z in r['audit_bootstrap'].values()) for r in results])
    order=np.argsort(pv);q=np.minimum.accumulate((pv[order]*len(pv)/np.arange(1,len(pv)+1))[::-1])[::-1]
    for idx,value in zip(order,q):results[idx]['bh_q']=float(min(1,value))
    for r in results:
        s=r['selected'];a=r['partitions']['audit'][s];e=r['partitions']['extension_used'][s]
        positive_years=sum((r['years'][str(year)]['r2_mean'] or 0)>0 for year in (2020,2021,2022))
        r['candidate_prior']=a['r2_mean']>0 and e['r2_mean']>0 and positive_years>=2
        r['strong_evidence']=r['candidate_prior'] and r['bh_q']<.05 and all(z['ci95'][0]>0 for z in r['audit_bootstrap'].values())
    report={**frozen,'results':results,'protocol_sha256':hashlib.sha256(protocol).hexdigest()}
    (output/'result.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(json.dumps([{'task':r['task'],'selected':r['selected'],'audit':r['partitions']['audit'][r['selected']]['r2_mean'],
         'extension':r['partitions']['extension_used'][r['selected']]['r2_mean'],
         'candidate':r['candidate_prior'],'strong':r['strong_evidence']} for r in results],indent=2),flush=True)
    return report

if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('--output-dir',type=Path,default=Path('research/output/bonds_tuning'))
    run(cli.parse_args().output_dir)
