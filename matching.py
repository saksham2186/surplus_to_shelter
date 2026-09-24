"""Surplus-to-Shelter matching engine with smart scoring and impact analytics."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

@dataclass
class Donation:
    id: int
    donor_name: str
    description: str
    quantity_kg: float
    lat: float
    lng: float
    expires_at: datetime
    status: str = "posted"
    recipient_id: Optional[int] = None
    driver_id: Optional[int] = None
    category: str = "any"
    donor_phone: Optional[str] = None
    donor_email: Optional[str] = None
    meals: int = 0
    otp_pickup: Optional[str] = None
    otp_delivery: Optional[str] = None
    match_score: float = 0

@dataclass
class Recipient:
    id: int
    name: str
    lat: float
    lng: float
    capacity_kg: float
    accepts: set[str] = field(default_factory=lambda: {"any"})
    open_until: Optional[datetime] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    verified: bool = True
    people_served: int = 0

@dataclass
class Driver:
    id: int
    name: str
    lat: float
    lng: float
    available: bool = True
    phone: Optional[str] = None
    email: Optional[str] = None
    rating: float = 5.0
    deliveries: int = 0

AVG_SPEED_KMH = 25
SAFETY_BUFFER_MIN = 30
MEALS_PER_KG = 2.0
CO2E_KG_PER_KG_FOOD = 2.5

def haversine_km(lat1, lng1, lat2, lng2):
    r=6371.0; p1,p2=math.radians(lat1),math.radians(lat2); dp=p2-p1; dl=math.radians(lng2-lng1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))

def travel_minutes(km): return km / AVG_SPEED_KMH * 60

def category_match(d, r):
    if not r.accepts or "any" in r.accepts or d.category in r.accepts: return 1.0
    return 0.0

def score_recipient(d, r, now=None):
    now=now or datetime.now()
    if r.capacity_kg < d.quantity_kg or category_match(d,r) == 0: return None
    km=haversine_km(d.lat,d.lng,r.lat,r.lng)
    eta=now+timedelta(minutes=travel_minutes(km)+15)
    deadline=d.expires_at-timedelta(minutes=SAFETY_BUFFER_MIN)
    if eta>deadline or (r.open_until and eta>r.open_until): return None
    distance_score=max(0,1-km/15)*45
    fit_score=min(25,(d.quantity_kg/max(r.capacity_kg,1))*25)
    category_score=15*category_match(d,r)
    urgency=15 if deadline-eta<timedelta(hours=1) else 0
    return round(min(100,distance_score+fit_score+category_score+urgency),2)

def match_donation(d, recipients, now=None):
    now=now or datetime.now(); best=None; best_score=-1
    for r in recipients:
        s=score_recipient(d,r,now)
        if s is not None and s>best_score: best,best_score=r,s
    return (best,best_score) if best else None

def assign_driver(d, drivers):
    free=[x for x in drivers if x.available]
    return min(free,key=lambda x:haversine_km(x.lat,x.lng,d.lat,d.lng)) if free else None

def impact(donations):
    done=[d for d in donations if d.status=="delivered"]; kg=sum(d.quantity_kg for d in done)
    return {"donations_delivered":len(done),"weight_diverted_kg":round(kg,1),"meals_rescued":int(kg*MEALS_PER_KG),"co2e_avoided_kg":round(kg*CO2E_KG_PER_KG_FOOD,1),"active_donations":sum(d.status in {"matched","picked_up"} for d in donations)}
