"""Exploratory same-index carry prior; gross return and excess over short bonds."""
from pathlib import Path
import hashlib
import json
import numpy as np
from bond_tuning import labels_for, rolling_mean, ridge_predictions, mask_partition, summary, bootstrap_improvement, PARTITIONS, economic
from bond_research import load,series


def run(root=Path('research/output/bonds_tuning')):
    panel=np.load(root/'data_panel.npz');dates=panel['dates'];inputs=root/'inputs'
    manifests={}
    def get(name,metric):
        path=inputs/f'index_{name}_{metric}.json';manifests[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        mapping=series(load(path)['series'])
        return np.array([mapping.get(str(d),np.nan) for d in dates])
    short=get('short','财富');short_yield=get('short','平均市值法到期收益率')
    allrank=[];choices=[];jobs=[]
    for target in ('3-5Y','7-10Y'):
        values=panel[target];ytm=get(target,'平均市值法到期收益率');duration=get(target,'平均市值法久期')
        fixed=np.column_stack([ytm,duration,ytm-short_yield])
        mom=np.column_stack([np.r_[np.full(k,np.nan),(values[k:]/values[:-k]-1)*10000] for k in (5,20,60,120)])
        groups={'index':fixed,'curve':np.column_stack([fixed,panel['curve']]),
                'all':np.column_stack([fixed,panel['curve'],mom])}
        for kind in ('gross','excess_short'):
            for h in (20,60,120):
                key=f'{target}_{kind}_{h}';print('carry',key,flush=True)
                y=labels_for(values,h,False)
                if kind=='excess_short':y-=labels_for(short,h,False)
                base=rolling_mean(y,h,756)
                carry=(ytm-(short_yield if kind=='excess_short' else 0))*100*h/252
                ps={'zero':np.zeros(len(y)),'mean_756':base}
                for weight in (.25,.5,1.):ps[f'carry_{weight}']=(1-weight)*base+weight*carry
                for group,x in groups.items():
                    for window in (504,756,1260):
                        for alpha in (10.,100.,1000.):
                            pred=ridge_predictions(x,y,h,window,alpha)
                            for weight in (.25,.5,1.):
                                ps[f'ridge_{group}_{window}_{alpha}_{weight}']=(1-weight)*base+weight*pred
                rank=[]
                for name,p in ps.items():
                    stats={part:summary(y,p,base,mask_partition(dates,h,part)) for part in ('validation1','validation2')}
                    if any(stats[part]['n']!=summary(y,base,base,mask_partition(dates,h,part))['n'] for part in stats):continue
                    rank.append({'task':key,'model':name,'selection_loss_ratio':1-float(np.mean([v['r2_mean'] for v in stats.values()])),**stats})
                selected=min(rank,key=lambda z:(z['selection_loss_ratio'],z['model']))['model']
                carry_selected=min((r for r in rank if r['model'].startswith('carry')),key=lambda z:z['selection_loss_ratio'])['model']
                choices.append({'task':key,'selected':selected,'carry_selected':carry_selected,'candidate_count':len(rank)})
                allrank.extend(rank);jobs.append((key,h,y,base,ps,selected,carry_selected,kind))
                np.savez_compressed(root/f'carry_predictions_{key}.npz',actual=y,**ps)
    (root/'carry_selection_frozen.json').write_text(json.dumps({'choices':choices,'input_sha256':manifests},indent=2))
    (root/'carry_validation_rankings.json').write_text(json.dumps(allrank,indent=2))
    results=[]
    for key,h,y,base,ps,selected,carry_selected,kind in jobs:
        item={'task':key,'selected':selected,'carry_selected':carry_selected,'results':{},'status':'post_inspection_exploratory'}
        for name in dict.fromkeys((selected,carry_selected,'carry_1.0','mean_756','zero')):
            p=ps[name];r={'partitions':{part:summary(y,p,base,mask_partition(dates,h,part)) for part in PARTITIONS},'years':{}}
            mask=mask_partition(dates,h,'audit')&np.isfinite(y)&np.isfinite(base)&np.isfinite(p)
            r['bootstrap']=bootstrap_improvement(y[mask],p[mask],base[mask],h)
            for year in range(2020,2027):
                m=mask_partition(dates,h,'audit' if year<2023 else 'extension_used')&np.char.startswith(dates,str(year))
                r['years'][str(year)]=summary(y,p,base,m)
            r['nonoverlap_r2']=[summary(y,p,base,mask&(np.arange(len(y))%h==offset))['r2_mean'] for offset in range(h)]
            r['candidate_prior']=r['partitions']['audit']['r2_mean']>0 and r['partitions']['extension_used']['r2_mean']>0 and sum((r['years'][str(z)]['r2_mean'] or 0)>0 for z in (2020,2021,2022))>=2
            if kind=='gross':r['economics']=economic(dates,y,p,base,mask,h)
            # For excess labels, cash means holding the short bond index. Do not
            # compound excess differences as an actual long-short NAV.
            item['results'][name]=r
        results.append(item)
    for selector in ('selected','carry_selected'):
        pv=np.array([max(z['p_one_sided'] for z in r['results'][r[selector]]['bootstrap'].values()) for r in results])
        order=np.argsort(pv);q=np.minimum.accumulate((pv[order]*len(pv)/np.arange(1,len(pv)+1))[::-1])[::-1]
        for idx,v in zip(order,q):results[idx][f'{selector}_bh_q']=min(1,float(v))
    report={'choices':choices,'input_sha256':manifests,'status':'exploratory_after_audit_inspection',
            'benchmark':'1年以下短债综合财富指数，含久期和信用风险，不是现金或融资成本','results':results}
    (root/'carry_result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for r in results:
        print(r['task'],[(name,round(z['partitions']['audit']['r2_mean'],4),round(z['partitions']['extension_used']['r2_mean'],4),z['candidate_prior']) for name,z in r['results'].items()],flush=True)

if __name__=='__main__':run()
