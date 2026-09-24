"""Surplus-to-Shelter advanced FastAPI demo.
Run: python -m uvicorn main:app --reload
Optional real SMS/email providers are configured through environment variables.
"""
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
import hashlib, os, random, secrets, smtplib, ssl, json
from email.message import EmailMessage
from urllib.parse import urlencode
from urllib.request import Request, urlopen, HTTPError
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from matching import Donation, Driver, Recipient, assign_driver, impact, match_donation, haversine_km, travel_minutes

app=FastAPI(title="Surplus-to-Shelter Advanced")

def seed():
    recipients=[
      Recipient(1,"Hope Shelter (C-Scheme)",26.9070,75.8000,40,{"cooked meal","bakery","any"},phone="+919999000001",email="hope@example.org",people_served=80),
      Recipient(2,"City Food Bank (Raja Park)",26.9010,75.8330,25,{"packaged goods","produce","any"},phone="+919999000002",email="foodbank@example.org",people_served=60),
      Recipient(3,"Seva Kitchen (Mansarovar)",26.8500,75.7600,60,{"cooked meal","produce","bakery","any"},phone="+919999000003",email="seva@example.org",people_served=120),
      Recipient(4,"Old Age Home (Malviya Nagar)",26.8549,75.8243,15,{"cooked meal","bakery","any"},phone="+919999000004",email="oldage@example.org",people_served=35)]
    drivers=[Driver(1,"Ravi",26.9100,75.7900,True,"+919999100001","ravi@example.org"),Driver(2,"Meena",26.8800,75.8100,True,"+919999100002","meena@example.org"),Driver(3,"Arjun",26.8600,75.7800,True,"+919999100003","arjun@example.org")]
    return recipients,drivers,[]
recipients,drivers,donations=seed(); photos={}; users={}; notifications=[]
# Latest GPS position reported by each driver. In production, persist this in a database/cache.
driver_locations={}
emergency_requests=[]

def otp(): return str(random.randint(100000,999999))
def pw(v): return hashlib.sha256(v.encode()).hexdigest()

def notify(kind, to, subject, message):
    """Send real notifications. Email prefers Resend API, then SMTP; SMS uses Twilio."""
    sent=False; detail="demo mode"
    if kind=="email" and to and os.getenv("RESEND_API_KEY"):
        try:
            payload=json.dumps({
                "from": os.getenv("EMAIL_FROM", "onboarding@resend.dev"),
                "to": [to],
                "subject": subject,
                "html": f"<div style='font-family:Arial,sans-serif;line-height:1.6'><h2>🍲 Surplus-to-Shelter</h2><p>{message.replace(chr(10), '<br>')}</p></div>",
            }).encode()
            req=Request("https://api.resend.com/emails",data=payload,headers={"Authorization":f"Bearer {os.getenv('RESEND_API_KEY')}","Content-Type":"application/json","Accept":"application/json","User-Agent":"Surplus-to-Shelter/1.0"},method="POST")
            with urlopen(req,timeout=15) as resp:
                result=json.loads(resp.read().decode())
            sent=True; detail=f"email sent via Resend: {result.get('id','accepted')}"
        except HTTPError as e:
            # Resend returns the useful reason in the JSON response body. The
            # old code only showed "HTTP Error 403", which hid the real cause.
            try:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    err = json.loads(raw)
                    msg = err.get("message") or err.get("error") or raw
                    code = err.get("name") or err.get("code")
                    detail = f"Resend HTTP {e.code}: {code + ' - ' if code else ''}{msg}"
                except json.JSONDecodeError:
                    detail = f"Resend HTTP {e.code}: {raw or e.reason}"
            except Exception:
                detail = f"Resend HTTP {e.code}: {e.reason}"
        except Exception as e:
            detail=f"Resend email failed: {e}"

    # If Resend is still in its testing-only mode (or otherwise rejects the
    # recipient), fall back to Gmail SMTP. This allows the app to send to
    # normal user addresses once a Gmail App Password is configured.
    if kind=="email" and to and not sent and os.getenv("GMAIL_USER") and os.getenv("GMAIL_APP_PASSWORD"):
        try:
            gmail_user=os.getenv("GMAIL_USER", "").strip()
            gmail_password=os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
            msg=EmailMessage()
            msg["Subject"]=subject
            msg["From"]=os.getenv("GMAIL_FROM", gmail_user)
            msg["To"]=to
            msg.set_content(message)
            with smtplib.SMTP("smtp.gmail.com",587,timeout=15) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                s.login(gmail_user,gmail_password)
                s.send_message(msg)
            sent=True
            detail="email sent via Gmail SMTP"
        except Exception as e:
            detail=f"Gmail SMTP failed: {e}"

    elif kind=="email" and to and os.getenv("SMTP_HOST"):
        try:
            msg=EmailMessage(); msg["Subject"]=subject; msg["From"]=os.getenv("SMTP_FROM",os.getenv("SMTP_USER","")); msg["To"]=to; msg.set_content(message)
            with smtplib.SMTP(os.getenv("SMTP_HOST"),int(os.getenv("SMTP_PORT","587")),timeout=10) as s:
                s.starttls(context=ssl.create_default_context()); s.login(os.getenv("SMTP_USER",""),os.getenv("SMTP_PASSWORD","")); s.send_message(msg)
            sent=True; detail="email sent"
        except Exception as e: detail=f"email failed: {e}"
    elif kind=="sms" and to and os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_FROM"):
        try:
            url=f"https://api.twilio.com/2010-04-01/Accounts/{os.getenv('TWILIO_ACCOUNT_SID')}/Messages.json"
            data=urlencode({"To":to,"From":os.getenv("TWILIO_FROM"),"Body":message}).encode()
            import base64
            auth=base64.b64encode(f"{os.getenv('TWILIO_ACCOUNT_SID')}:{os.getenv('TWILIO_AUTH_TOKEN')}".encode()).decode()
            req=Request(url,data=data,headers={"Authorization":f"Basic {auth}"}); urlopen(req,timeout=10).read(); sent=True; detail="sms sent"
        except Exception as e: detail=f"sms failed: {e}"
    notifications.append({"time":datetime.now().isoformat(),"kind":kind,"to":to,"subject":subject,"message":message,"sent":sent,"detail":detail})
    return {"sent":sent,"detail":detail}

def find(items,item_id,label):
    for x in items:
        if x.id==item_id:return x
    raise HTTPException(404,f"{label} {item_id} not found")

def donation_view(d):
    out=asdict(d); out["expires_at"]=d.expires_at.isoformat(); out["photo"]=photos.get(d.id)
    r=next((x for x in recipients if x.id==d.recipient_id),None); drv=next((x for x in drivers if x.id==d.driver_id),None)
    out["recipient"]=r.name if r else None; out["driver"]=drv.name if drv else None
    if r: out["recipient_phone"]=r.phone
    if drv: out["driver_phone"]=drv.phone
    out["minutes_left"]=max(0,round((d.expires_at-datetime.now()).total_seconds()/60)); return out

def recipient_view(r):
    o=asdict(r); o["accepts"]=sorted(r.accepts); o["open_until"]=r.open_until.isoformat() if r.open_until else None; return o

class DonationIn(BaseModel):
    donor_name:str; description:str; quantity_kg:float=Field(gt=0); lat:float; lng:float; expires_in_minutes:int=Field(default=180,ge=15); photo:str|None=None; category:str="any"; donor_phone:str|None=None; donor_email:str|None=None
class AuthIn(BaseModel): name:str; email:str; password:str; role:str="donor"; phone:str|None=None
class LoginIn(BaseModel): email:str; password:str
class OTPIn(BaseModel): otp:str
class ProfileIn(BaseModel): phone:str|None=None; email:str|None=None; name:str|None=None
class CapacityIn(BaseModel): capacity_kg:float=Field(ge=0)
class LocationIn(BaseModel): lat:float; lng:float
class TrackingOut(BaseModel): driver_id:int; lat:float; lng:float; updated_at:str
class FoodCheckIn(BaseModel):
    description:str
    category:str="any"
    expires_in_minutes:int=Field(default=180,ge=15)
class EmergencyIn(BaseModel):
    shelter_id:int
    food_type:str="any"
    quantity_kg:float=Field(gt=0)
    people:int=Field(default=10,ge=1)
    urgency:str="high"
    note:str=""
class QRVerifyIn(BaseModel):
    token:str

@app.get("/")
def home(): return FileResponse(Path(__file__).parent/"index.html")

@app.post("/auth/signup")
def signup(b:AuthIn):
    if b.email.lower() in users: raise HTTPException(409,"Email already registered")
    users[b.email.lower()]={"id":len(users)+1,"name":b.name,"email":b.email.lower(),"password":pw(b.password),"role":b.role,"phone":b.phone,"verified":False}
    return {k:v for k,v in users[b.email.lower()].items() if k!="password"}
@app.post("/auth/login")
def login(b:LoginIn):
    u=users.get(b.email.lower())
    if not u or u["password"]!=pw(b.password): raise HTTPException(401,"Invalid email or password")
    return {k:v for k,v in u.items() if k!="password"}
@app.post("/auth/demo")
def demo_login(): return {"id":0,"name":"Demo User","email":"demo@surplus.local","role":"donor","verified":True}

@app.get("/notifications/config")
def notification_config():
    return {
        "resend_configured": bool(os.getenv("RESEND_API_KEY")),
        "email_from": os.getenv("EMAIL_FROM", "onboarding@resend.dev"),
        "smtp_configured": bool(os.getenv("SMTP_HOST")),
        "gmail_configured": bool(os.getenv("GMAIL_USER") and os.getenv("GMAIL_APP_PASSWORD")),
    }

@app.post("/notifications/test")
def test_notification(kind:str="email",to:str=""):
    return notify(kind,to,"Surplus-to-Shelter notification","This is a test notification from your Surplus-to-Shelter demo.")
@app.get("/notifications")
def get_notifications(): return notifications[-50:][::-1]

def _send_donor_email_async(email, donor_name, quantity_kg, donation_id):
    try:
        notify(
            "email",
            email,
            f"Donation posted successfully #{donation_id}",
            f"Hi {donor_name}, your donation of {quantity_kg} kg has been received successfully. "
            f"Donation ID: #{donation_id}. We are now finding a suitable recipient and pickup driver."
        )
    except Exception:
        pass

@app.post("/donations")
def create_donation(b:DonationIn, background_tasks:BackgroundTasks):
    if not b.donor_email:
        raise HTTPException(400,"Please enter donor email so the confirmation mail can be sent immediately.")

    now=datetime.now(); d=Donation(len(donations)+1,b.donor_name,b.description,b.quantity_kg,b.lat,b.lng,now+timedelta(minutes=b.expires_in_minutes),category=b.category,donor_phone=b.donor_phone,donor_email=b.donor_email,meals=int(b.quantity_kg*2),otp_pickup=otp(),otp_delivery=otp())
    donations.append(d)
    if b.photo and len(b.photo)<600000: photos[d.id]=b.photo

    # Queue the donor email in the background so the donation/matching response
    # is not blocked by Gmail/SMTP network latency.
    background_tasks.add_task(_send_donor_email_async, d.donor_email, d.donor_name, d.quantity_kg, d.id)
    email_result={"sent":True,"detail":"email queued for background delivery"}

    result=match_donation(d,recipients,now)
    if result is None: d.status="unmatched"
    else:
        r,score=result; d.recipient_id=r.id; d.match_score=score; r.capacity_kg-=d.quantity_kg
        drv=assign_driver(d,drivers)
        if drv: d.driver_id=drv.id; drv.available=False
        d.status="matched"
    if d.status=="matched":
        r=next(x for x in recipients if x.id==d.recipient_id); drv=next((x for x in drivers if x.id==d.driver_id),None)
        msg=f"Donation #{d.id} matched to {r.name}. Pickup OTP: {d.otp_pickup}. Match score: {d.match_score}/100."
        notify("sms",d.donor_phone,"Donation matched",msg)
        if drv: notify("sms",drv.phone,f"New pickup #{d.id}",f"Pickup {d.quantity_kg}kg from {d.donor_name}. OTP {d.otp_pickup}.")
    else:
        # No second donor email here: the immediate confirmation above is the instant mail.
        pass
    response=donation_view(d)
    response["email_sent"]=email_result.get("sent",False)
    response["email_detail"]=email_result.get("detail","")
    return response

@app.get("/donations")
def list_donations(): return [donation_view(d) for d in donations]

@app.post("/donations/{donation_id}/pickup")
def mark_picked_up(donation_id:int,b:OTPIn|None=None):
    d=find(donations,donation_id,"Donation")
    if d.status!="matched": raise HTTPException(400,f"Cannot pick up a donation with status '{d.status}'")
    if b and b.otp and b.otp!=d.otp_pickup: raise HTTPException(400,"Invalid pickup OTP")
    d.status="picked_up"; notify("sms",d.donor_phone,f"Pickup confirmed #{d.id}","Your surplus food has been picked up successfully."); return donation_view(d)
@app.post("/donations/{donation_id}/deliver")
def mark_delivered(donation_id:int,b:OTPIn|None=None):
    d=find(donations,donation_id,"Donation")
    if d.status!="picked_up": raise HTTPException(400,f"Cannot deliver a donation with status '{d.status}'")
    if b and b.otp and b.otp!=d.otp_delivery: raise HTTPException(400,"Invalid delivery OTP")
    d.status="delivered"; dvr=find(drivers,d.driver_id,"Driver") if d.driver_id else None
    if dvr: dvr.available=True; dvr.deliveries+=1
    notify("email",d.donor_email,f"Donation delivered #{d.id}","Your food donation has reached the recipient. Thank you!"); return donation_view(d)
@app.post("/donations/{donation_id}/cancel")
def cancel(donation_id:int):
    d=find(donations,donation_id,"Donation")
    if d.status in {"delivered","cancelled"}: raise HTTPException(400,"Donation cannot be cancelled")
    if d.recipient_id:
        r=find(recipients,d.recipient_id,"Recipient"); r.capacity_kg+=d.quantity_kg
    if d.driver_id: find(drivers,d.driver_id,"Driver").available=True
    d.status="cancelled"; return donation_view(d)

@app.get("/recipients")
def list_recipients(): return [recipient_view(r) for r in recipients]
@app.patch("/recipients/{recipient_id}/capacity")
def update_capacity(recipient_id:int,b:CapacityIn):
    r=find(recipients,recipient_id,"Recipient"); r.capacity_kg=b.capacity_kg; return recipient_view(r)
@app.get("/drivers")
def list_drivers(): return [asdict(x) for x in drivers]
@app.patch("/drivers/{driver_id}/location")
def driver_location(driver_id:int,b:LocationIn):
    d=find(drivers,driver_id,"Driver"); d.lat=b.lat; d.lng=b.lng
    driver_locations[driver_id]={"lat":b.lat,"lng":b.lng,"updated_at":datetime.now().isoformat()}
    return {**asdict(d),"location_updated_at":driver_locations[driver_id]["updated_at"]}

@app.get("/drivers/{driver_id}/location")
def get_driver_location(driver_id:int):
    d=find(drivers,driver_id,"Driver")
    loc=driver_locations.get(driver_id,{"lat":d.lat,"lng":d.lng,"updated_at":None})
    return {"driver_id":driver_id,"driver":d.name,"lat":loc["lat"],"lng":loc["lng"],"updated_at":loc["updated_at"],"available":d.available}

@app.get("/donations/{donation_id}/tracking")
def donation_tracking(donation_id:int):
    d=find(donations,donation_id,"Donation")
    r=next((x for x in recipients if x.id==d.recipient_id),None)

    # Tracking can be opened before a driver has been assigned. Return a
    # normal response instead of a 404 so the dashboard does not show an
    # API error for a perfectly valid donation state.
    if not d.driver_id:
        return {
            "donation_id":d.id, "status":d.status, "tracking_available":False,
            "driver_id":None, "driver":None, "driver_lat":None, "driver_lng":None,
            "location_updated_at":None, "donor_lat":d.lat, "donor_lng":d.lng,
            "recipient_lat":r.lat if r else None, "recipient_lng":r.lng if r else None,
            "message":"Driver has not been assigned yet."
        }

    drv=find(drivers,d.driver_id,"Driver")
    loc=driver_locations.get(drv.id,{"lat":drv.lat,"lng":drv.lng,"updated_at":None})
    return {
        "donation_id":d.id,"status":d.status,"tracking_available":True,
        "driver_id":drv.id,"driver":drv.name,"driver_lat":loc["lat"],"driver_lng":loc["lng"],
        "location_updated_at":loc["updated_at"],"donor_lat":d.lat,"donor_lng":d.lng,
        "recipient_lat":r.lat if r else None,"recipient_lng":r.lng if r else None
    }
@app.patch("/drivers/{driver_id}/availability")
def driver_availability(driver_id:int,available:bool=True):
    d=find(drivers,driver_id,"Driver"); d.available=available; return asdict(d)

@app.get("/impact")
def get_impact(): return impact(donations)
@app.get("/analytics")
def analytics():
    cats={}; statuses={}
    for d in donations: cats[d.category]=cats.get(d.category,0)+1; statuses[d.status]=statuses.get(d.status,0)+1
    return {"categories":cats,"statuses":statuses,"avg_match_score":round(sum(d.match_score for d in donations if d.match_score)/max(1,sum(bool(d.match_score) for d in donations)),1),"total":len(donations)}
@app.get("/donations/{donation_id}/route")
def route(donation_id:int):
    d=find(donations,donation_id,"Donation"); r=next((x for x in recipients if x.id==d.recipient_id),None); drv=next((x for x in drivers if x.id==d.driver_id),None)
    return {"donor":{"lat":d.lat,"lng":d.lng},"driver":{"lat":drv.lat,"lng":drv.lng} if drv else None,"recipient":{"lat":r.lat,"lng":r.lng} if r else None,"driver_to_donor_km":round(haversine_km(drv.lat,drv.lng,d.lat,d.lng),2) if drv else None,"donor_to_recipient_km":round(haversine_km(d.lat,d.lng,r.lat,r.lng),2) if r else None,"eta_minutes":round(travel_minutes(haversine_km(d.lat,d.lng,r.lat,r.lng))+15) if r else None}

@app.post("/features/ai-food-check")
def ai_food_check(b:FoodCheckIn):
    """Fast local food-photo/description assistant. It is a heuristic, not a food-safety certification."""
    text=(b.description or "").lower()
    rules={
        "cooked meal":["rice","dal","curry","meal","food","roti","chapati","biryani","sabzi","pasta","noodles"],
        "bakery":["bread","cake","bun","bakery","croissant","pastry","biscuit"],
        "produce":["fruit","vegetable","vegetables","apple","banana","tomato","potato","produce","salad"],
        "packaged goods":["packet","packaged","can","tin","sealed","snack","chips","cereal"],
        "beverages":["juice","milk","water","beverage","drink","tea","coffee"],
        "desserts":["dessert","sweet","gulab jamun","halwa","kheer","ice cream"],
    }
    scores={k:sum(1 for word in words if word in text) for k,words in rules.items()}
    suggested=max(scores,key=scores.get) if max(scores.values(),default=0)>0 else (b.category if b.category!="any" else "cooked meal")
    confidence=92 if scores.get(suggested,0)>=2 else (78 if scores.get(suggested,0)==1 else 65)
    warnings=[]
    if b.expires_in_minutes<=60: warnings.append("Very short expiry window — prioritize immediate pickup.")
    if suggested in {"cooked meal","beverages"}: warnings.append("Keep temperature control and hygiene checks during pickup.")
    warnings.append("Visual/description analysis is only a suggestion; donor must confirm food is safe to donate.")
    return {"suggested_category":suggested,"confidence":confidence,"quality":"Review required","checks":["Packaging/cleanliness","Expiry time","Temperature where applicable","Allergen information"],"warnings":warnings}

@app.get("/features/leaderboard")
def leaderboard():
    totals={}
    delivered={}
    for d in donations:
        if d.status=="delivered":
            totals[d.donor_name]=totals.get(d.donor_name,0)+d.quantity_kg
            delivered[d.donor_name]=delivered.get(d.donor_name,0)+1
    rows=sorted(totals.items(),key=lambda x:x[1],reverse=True)[:10]
    return [{"rank":i+1,"name":name,"kg":round(kg,1),"deliveries":delivered.get(name,0),"badge":"Community Champion" if kg>=100 else ("Food Hero" if kg>=25 else "First Rescuer")} for i,(name,kg) in enumerate(rows)]

@app.get("/features/carbon")
def carbon_impact():
    delivered_kg=sum(d.quantity_kg for d in donations if d.status=="delivered")
    return {"food_kg":round(delivered_kg,1),"co2e_kg":round(delivered_kg*2.5,1),"meals":int(delivered_kg*2),"method":"Estimated using 2.5 kg CO2e per kg food diverted; illustrative project metric."}

@app.get("/features/expiry-alerts")
def expiry_alerts():
    now=datetime.now(); alerts=[]
    for d in donations:
        if d.status in {"delivered","cancelled"}: continue
        mins=max(0,round((d.expires_at-now).total_seconds()/60))
        if mins<=60:
            alerts.append({"donation_id":d.id,"food":d.description,"minutes_left":mins,"quantity_kg":d.quantity_kg,"status":d.status,"severity":"critical" if mins<=20 else "high"})
    return sorted(alerts,key=lambda x:x["minutes_left"])

@app.get("/features/demand")
def demand_prediction():
    demand={r.id:{"shelter":r.name,"area":r.name.split("(")[-1].rstrip(")"),"current_capacity_kg":round(r.capacity_kg,1),"needs":{}} for r in recipients}
    for e in emergency_requests:
        if e["shelter_id"] in demand:
            demand[e["shelter_id"]]["needs"][e["food_type"]]=demand[e["shelter_id"]]["needs"].get(e["food_type"],0)+e["quantity_kg"]
    historical={}
    for d in donations:
        historical[d.category]=historical.get(d.category,0)+d.quantity_kg
    common=sorted(historical.items(),key=lambda x:x[1],reverse=True)
    for row in demand.values():
        if not row["needs"]:
            row["needs"]={cat:round(max(5,kg*0.35),1) for cat,kg in common[:3]} if common else {"cooked meal":20,"produce":10}
    return {"forecast":list(demand.values()),"note":"Forecast uses current capacity, emergency requests and recent donation mix; it is a demo planning estimate."}

@app.post("/features/emergency")
def create_emergency(b:EmergencyIn):
    r=find(recipients,b.shelter_id,"Recipient")
    item={"id":len(emergency_requests)+1,"shelter_id":r.id,"shelter":r.name,"food_type":b.food_type,"quantity_kg":b.quantity_kg,"people":b.people,"urgency":b.urgency,"note":b.note,"created_at":datetime.now().isoformat(),"status":"open"}
    emergency_requests.append(item)
    notify("email",r.email,"Emergency food request created",f"Emergency request: {b.quantity_kg} kg of {b.food_type} for about {b.people} people. Urgency: {b.urgency}.") if r.email else None
    return item

@app.get("/features/emergency")
def list_emergency():
    return sorted(emergency_requests,key=lambda x:(x["status"]!="open",x["urgency"]!="critical",x["created_at"]))

@app.post("/features/emergency/{request_id}/close")
def close_emergency(request_id:int):
    item=next((x for x in emergency_requests if x["id"]==request_id),None)
    if not item: raise HTTPException(404,"Emergency request not found")
    item["status"]="fulfilled"; item["closed_at"]=datetime.now().isoformat(); return item

@app.get("/features/route/{driver_id}")
def optimized_route(driver_id:int):
    drv=find(drivers,driver_id,"Driver")
    active=[d for d in donations if d.status in {"matched","picked_up"} and d.driver_id==driver_id]
    remaining=active[:]; cur=(drv.lat,drv.lng); stops=[]; total=0.0
    while remaining:
        nxt=min(remaining,key=lambda d:haversine_km(cur[0],cur[1],d.lat,d.lng))
        km=haversine_km(cur[0],cur[1],nxt.lat,nxt.lng); total+=km
        stops.append({"donation_id":nxt.id,"food":nxt.description,"donor":nxt.donor_name,"lat":nxt.lat,"lng":nxt.lng,"km_from_previous":round(km,2)})
        cur=(nxt.lat,nxt.lng); remaining.remove(nxt)
    return {"driver_id":driver_id,"driver":drv.name,"stops":stops,"total_km":round(total,2),"eta_minutes":round(travel_minutes(total),1)}

@app.get("/features/qr/{donation_id}")
def pickup_qr(donation_id:int):
    d=find(donations,donation_id,"Donation")
    return {"donation_id":d.id,"token":f"PICKUP-{d.id}-{d.otp_pickup}","purpose":"pickup","otp":d.otp_pickup}

@app.post("/features/qr/{donation_id}/verify")
def verify_qr(donation_id:int,b:QRVerifyIn):
    d=find(donations,donation_id,"Donation")
    expected=f"PICKUP-{d.id}-{d.otp_pickup}"
    if secrets.compare_digest(b.token.strip(),expected):
        if d.status=="matched": d.status="picked_up"; notify("email",d.donor_email,f"QR pickup verified #{d.id}","Your donation pickup was verified digitally.") if d.donor_email else None
        return {"verified":True,"status":d.status,"message":"Pickup QR verified successfully."}
    raise HTTPException(400,"Invalid pickup QR token")

@app.post("/reset")
def reset():
    global recipients,drivers,donations,driver_locations
    recipients,drivers,donations=seed(); photos.clear(); notifications.clear(); driver_locations.clear(); emergency_requests.clear(); return {"status":"reset"}
