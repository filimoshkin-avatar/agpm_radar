#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]; C=ROOT/'contracts/v1'; spec=yaml.safe_load((C/'sqlite-contract.yaml').read_text())
def col_schema(c):
 t={'TEXT':'string','INTEGER':'integer','REAL':'number'}[c['type']]; s={'type':[t,'null'] if c.get('nullable') else t}
 if 'enum' in c:s['enum']=c['enum']+([None] if c.get('nullable') else [])
 if 'minimum' in c:s['minimum']=c['minimum']
 f=c.get('format')
 if f=='sha256':s['pattern']='^[a-f0-9]{64}$'
 elif f in {'date','date-time','uri'}:s['format']=f
 elif f=='relative-path':s['pattern']='^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[A-Za-z0-9._/-]+$'
 return s
def obj_for(table, columns, required):
 return {'type':'object','additionalProperties':False,'required':required,'properties':{n:col_schema(columns[n]) for n in columns}}
def operation_schema(name,table,action):
 cols=table['columns']; pk=table['primaryKey']; props={'sequence':{'type':'integer','minimum':1},'action':{'const':action},'table':{'const':name},'key':obj_for(table,{k:cols[k] for k in pk},pk),'expectedBefore':{'oneOf':[{'const':'absent'},{'type':'string','pattern':'^[a-f0-9]{64}$'}]},'rowAfterHash':{'type':['string','null'],'pattern':'^[a-f0-9]{64}$'}}
 req=['sequence','action','table','key','expectedBefore','rowAfterHash']
 if action!='delete':props['values']=obj_for(table,cols,list(cols));req.append('values')
 if action=='insert':props['expectedBefore']={'const':'absent'}
 if action=='delete':props['rowAfterHash']={'type':'null'}
 else:props['rowAfterHash']={'type':'string','pattern':'^[a-f0-9]{64}$'}
 return {'type':'object','additionalProperties':False,'required':req,'properties':props}
ops=[]
for n,t in spec['tables'].items():
 for a in t.get('contentMutations',[]):ops.append(operation_schema(n,t,a))
all_tables=list(spec['tables'])
sha={'type':'string','pattern':'^[a-f0-9]{64}$'}; ident={'type':'string','pattern':'^[a-z0-9][a-z0-9._:-]{7,127}$'}
schema={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'https://radar.aipractice.space/contracts/v1/delta.schema.json','title':'Radar V2 publisher-generated typed delta','type':'object','additionalProperties':False,'required':['contractVersion','releaseId','candidateId','operation','applicationReleaseId','baseReleaseId','baseSequence','targetSequence','schemaVersionBefore','schemaVersionAfter','tableContractVersion','beforeStateHash','afterStateHash','createdAt','operations','expectedTables','assets'],'properties':{
'contractVersion':{'const':'1.0.0'},'releaseId':ident,'candidateId':ident,'operation':{'enum':['daily','correction','gazette']},'applicationReleaseId':ident,'baseReleaseId':{'type':['string','null']},'baseSequence':{'type':'integer','minimum':0},'targetSequence':{'type':'integer','minimum':1},'schemaVersionBefore':{'const':1},'schemaVersionAfter':{'const':1},'tableContractVersion':{'const':'1.0.0'},'beforeStateHash':sha,'afterStateHash':sha,'createdAt':{'type':'string','format':'date-time'},'operations':{'type':'array','minItems':1,'maxItems':10000,'items':{'oneOf':ops}},'expectedTables':{'type':'array','minItems':len(all_tables),'maxItems':len(all_tables),'items':{'type':'object','additionalProperties':False,'required':['table','beforeRowCount','afterRowCount','beforeLogicalSha256','afterLogicalSha256'],'properties':{'table':{'enum':all_tables},'beforeRowCount':{'type':'integer','minimum':0},'afterRowCount':{'type':'integer','minimum':0},'beforeLogicalSha256':sha,'afterLogicalSha256':sha}}},'assets':{'type':'array','maxItems':1000,'items':{'type':'object','additionalProperties':False,'required':['relativePath','sha256','bytes','mediaType'],'properties':{'relativePath':{'type':'string','pattern':'^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[A-Za-z0-9._/-]+$'},'sha256':sha,'bytes':{'type':'integer','minimum':0},'mediaType':{'type':'string'}}}}}}
(C/'delta.schema.json').write_text(json.dumps(schema,indent=2,sort_keys=True)+'\n')
print('generated',len(ops),'typed operation branches')
