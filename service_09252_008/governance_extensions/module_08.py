"""授权领域扩展。"""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from hashlib import sha256
import json
@dataclass(frozen=True)
class Record:
 key:str; owner:str; state:str; value:int; version:int=1; reason:str="created"
 def digest(self): return sha256(json.dumps(asdict(self),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
 def evolve(self,state,value,reason):
  if value<0 or not reason.strip(): raise ValueError("invalid change")
  return Record(self.key,self.owner,state,value,self.version+1,reason)
class Registry:
 def __init__(self): self.rows={}; self.history={}
 def open(self,key,owner,value):
  if key in self.rows: raise ValueError("duplicate key")
  row=Record(key,owner,"open",value); self.rows[key]=row; self.history[key]=[row]; return row
 def move(self,key,state,value=None,reason="transition"):
  old=self.rows[key]; row=old.evolve(state,old.value if value is None else value,reason); self.rows[key]=row; self.history[key].append(row); return row
 def get(self,key): return self.rows.get(key)
 def count(self,state=None): return sum(1 for r in self.rows.values() if state is None or r.state==state)
 def snapshot(self): return [asdict(self.rows[k]) for k in sorted(self.rows)]
 def verify(self): return all(len({x.digest() for x in rows})==len(rows) for rows in self.history.values())
