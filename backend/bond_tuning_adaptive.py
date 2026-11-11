"""Exploratory follow-up: prequential error-weighted priors; never live signals."""
from pathlib import Path
import json
import numpy as np
from bond_tuning import PARTITIONS, mask_partition, summary, bootstrap_improvement, economic


def adaptive_prediction(y, predictions, h, window, rule, strength=1., batch=20):
    names=list(predictions)
    matrix=np.column_stack([predictions[name] for name in names])
    common=np.isfinite(y)&np.isfinite(matrix).all(axis=1)
    losses=np.where(common[:,None],(matrix-y[:,None])**2,0.)
    cs=np.vstack([np.zeros(len(names)),np.cumsum(losses,axis=0)])
    counts=np.r_[0,np.cumsum(common)]
    base=predictions['mean_756']
    out=np.full(len(y),np.nan)
    for t in range(0,len(y),batch):
        end=max(0,t-h);start=max(0,end-window)
        n=counts[end]-counts[start]
        if n<126:continue
        err=(cs[end]-cs[start])/n
        if rule.startswith('top'):
            k=min(int(rule[3:]),len(names));weights=np.zeros(len(names))
            weights[np.argsort(err,kind='stable')[:k]]=1/k
        else:
            temperature=float(rule[3:]);scale=max(err[names.index('mean_756')],1e-8)
            weights=np.exp(-(err-err.min())/(temperature*scale));weights/=weights.sum()
        raw=matrix[t:t+batch]@weights
        out[t:t+batch]=(1-strength)*base[t:t+batch]+strength*raw
    return out


def run(root=Path('research/output/bonds_tuning')):
    first=json.loads((root/'result.json').read_text())
    dates=np.load(root/'data_panel.npz')['dates']
    results=[];rankings=[];choices=[];jobs=[]
    for choice in first['choices']:
        key=choice['task'];h=int(key.rsplit('_',1)[1]);print('adaptive',key,flush=True)
        data=np.load(root/f'predictions_{key}.npz')
        y=data['actual'];base=data['mean_756']
        groups={'all':lambda name:True,
                'price':lambda name:not name.startswith('ridge_') or name.startswith('ridge_momentum'),
                'reversal':lambda name:name.startswith(('mean_','zero','reversal_'))}
        ps={'mean_756':base};rows=[]
        for family,include in groups.items():
            models={name:data[name] for name in data.files if name not in ('actual','ensemble_top5') and include(name)}
            for window in (252,504,756,1260):
                for rule in ('top1','top5','top20','exp0.1','exp0.5','exp1.0'):
                    raw=adaptive_prediction(y,models,h,window,rule)
                    for strength in (.25,.5,1.):
                        name=f'{family}_{window}_{rule}_{strength}'
                        ps[name]=(1-strength)*base+strength*raw
        for name,p in ps.items():
            parts={part:summary(y,p,base,mask_partition(dates,h,part)) for part in ('validation1','validation2')}
            if not all(parts[part]['n']==summary(y,base,base,mask_partition(dates,h,part))['n'] for part in parts):
                continue
            rows.append({'task':key,'model':name,'selection_loss_ratio':1-np.mean([v['r2_mean'] for v in parts.values()]),**parts})
        best=min(rows,key=lambda row:(row['selection_loss_ratio'],row['model']))
        selections={}
        for family in ('all','price','reversal'):
            selections[family]=min((r for r in rows if r['model'].startswith(family)),key=lambda z:z['selection_loss_ratio'])['model']
        selections['global']=best['model']
        choices.append({'task':key,'selected':best['model'],'families':selections,'count':len(rows)})
        rankings.extend(rows);jobs.append((key,h,y,base,ps,selections))
        np.savez_compressed(root/f'adaptive_predictions_{key}.npz',actual=y,**ps)
    (root/'adaptive_selection_frozen.json').write_text(json.dumps(choices,indent=2))
    (root/'adaptive_validation_rankings.json').write_text(json.dumps(rankings,indent=2))
    for key,h,y,base,ps,selected in jobs:
        item={'task':key,'selections':selected,'results':{},'status':'post_inspection_exploratory'}
        for family,name in selected.items():
            p=ps[name]
            r={'selected':name,'partitions':{part:summary(y,p,base,mask_partition(dates,h,part)) for part in PARTITIONS},'years':{}}
            mask=mask_partition(dates,h,'audit')&np.isfinite(y)&np.isfinite(p)&np.isfinite(base)
            r['bootstrap']=bootstrap_improvement(y[mask],p[mask],base[mask],h)
            for year in range(2020,2027):
                m=mask_partition(dates,h,'audit' if year<2023 else 'extension_used')&np.char.startswith(dates,str(year))
                r['years'][str(year)]=summary(y,p,base,m)
            r['candidate_prior']=r['partitions']['audit']['r2_mean']>0 and r['partitions']['extension_used']['r2_mean']>0 and sum((r['years'][str(z)]['r2_mean'] or 0)>0 for z in (2020,2021,2022))>=2
            r['nonoverlap_r2']=[summary(y,p,base,mask&(np.arange(len(y))%h==offset))['r2_mean'] for offset in range(h)]
            if not key.startswith('yield'):
                r['economics']=economic(dates,y,p,base,mask,h)
            item['results'][family]=r
        results.append(item)
    # Primary nine dynamically selected claims, not a retrospective best-of-families.
    pv=np.array([max(z['p_one_sided'] for z in r['results']['global']['bootstrap'].values()) for r in results])
    order=np.argsort(pv);q=np.minimum.accumulate((pv[order]*len(pv)/np.arange(1,len(pv)+1))[::-1])[::-1]
    for i,v in zip(order,q):results[i]['results']['global']['bh_q']=min(1,float(v))
    report={'status':'exploratory_after_audit_inspection','choices':choices,'results':results}
    (root/'adaptive_result.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    for r in results:
        print(r['task'],[(k,v['selected'],round(v['partitions']['audit']['r2_mean'],4),round(v['partitions']['extension_used']['r2_mean'],4),v['candidate_prior']) for k,v in r['results'].items()],flush=True)

if __name__=='__main__':run()
