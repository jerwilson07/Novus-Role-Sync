"""RPP progress evidence from the published Novus requirements.
Final raid stage is KC-based; clan teams and voice chat are encouraged,
not mandatory proof. Other boss objectives still require mentor/recorded
proof and staff approval.
"""
RPP_CA = (
    (651, "Dancing Queen", "The Mad Angel"),
    (652, "Smite Fight", "The Mad Angel"),
    (648, "Perfect Mad Angel", "The Mad Angel"),
    (500, "Perfect Whisperer", "The Whisperer"),
    (102, "Perfect Corrupted Hunllef", "Corrupted Hunllef"),
    (106, "Wolf Puncher II", "Corrupted Hunllef"),
    (606, "Fire Fighter", "Yama"),
    (605, "Shadow Dancer", "Yama"),
)
# The published CG baseline says 5-10 minimum. Report both ends,
# never silently choose one as the definitive graduation threshold.
RPP_KC = (
    ("Mad Angel", "mad_angel", 25, 25),
    ("Whisperer", "the_whisperer", 10, 10),
    ("Leviathan", "the_leviathan", 20, 20),
    ("Corrupted Gauntlet", "the_corrupted_gauntlet", 5, 10),
    ("Yama", "yama", 10, 10),
    ("CoX", "chambers_of_xeric", 5, 5),
    ("ToB", "theatre_of_blood", 5, 5),
    ("Expert ToA", "tombs_of_amascut_expert", 10, 10),
)
MANUAL_PROOF = (
    "Whisperer: block volleys and avoid tentacles outside enrage",
    "Whisperer: no volley or tentacle damage during enrage in separate KC",
    "Leviathan: no shadow barrage until after first melee attack cycle",
    "Leviathan: five stacked boulders and no final explosion damage",
    "Leviathan: full lightning phase skip",
    "Corrupted Gauntlet: demonstrate 5:1 method",
)

def ca_progress(payload):
    rows = payload.get("data",[]) if isinstance(payload,dict) else []
    if not isinstance(rows,list):
        rows=[]
    result=[]
    for index,name,monster in RPP_CA:
        matches=[r for r in rows if isinstance(r,dict) and r.get("index")==index]
        task=matches[0] if len(matches)==1 else {}
        valid=(str(task.get("name","")).casefold()==name.casefold()
            and str(task.get("monster","")).casefold()==monster.casefold())
        state="unverified"
        if valid and task.get("exempt") is False and type(task.get("completed")) is bool:
            state="complete" if task["completed"] else "incomplete"
        elif valid and task.get("exempt") is True:
            state="exempt_needs_review"
        result.append({"id":index,"name":name,"boss":monster,"status":state})
    return result

def rpp_progress(values, payload):
    values=values or {}
    kc=[]
    for name,key,low,high in RPP_KC:
        value=values.get(key)
        if type(value) is not int or value<0:
            value=None
        status=("unverified" if value is None else
            "below_baseline" if value<low else
            "baseline_range_review" if value<high else "baseline_met")
        kc.append({"name":name,"kc":value,"minimum":low,"upper":high,"status":status})
    ca=ca_progress(payload)
    return {"ca_completed":sum(x["status"]=="complete" for x in ca),
            "ca_total":len(ca),"ca":ca,"kc":kc,
            "manual_proof_required":list(MANUAL_PROOF),
            "staff_approval_required":True}
