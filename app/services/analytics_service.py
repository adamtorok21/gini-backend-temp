import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from app.db.session import db

logger = logging.getLogger(__name__)

class AnalyticsService:
    """Service for generating analytics and reports"""
    
    async def get_villa_performance(
        self,
        villa_code: Optional[str] = None,
        days: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Get performance metrics for villas
        
        Args:
            villa_code: Specific villa code or None for all villas
            days: Number of days to analyze
            
        Returns:
            List of villa performance metrics
        """
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Build match stage
            match_stage = {
                "created_at": {"$gte": start_date}
            }
            if villa_code:
                match_stage["villa_code"] = villa_code
            
            # Aggregation pipeline
            pipeline = [
                {"$match": match_stage},
                {
                    "$group": {
                        "_id": "$villa_code",
                        "total_bookings": {"$sum": 1},
                        "completed_bookings": {
                            "$sum": {
                                "$cond": [{"$eq": ["$status", "payment_completed"]}, 1, 0]
                            }
                        },
                        "total_revenue": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$status", "payment_completed"]},
                                    {"$toInt": {"$arrayElemAt": [{"$split": ["$price", " "]}, 1]}},
                                    0
                                ]
                            }
                        },
                        "unique_guests": {"$addToSet": "$sender_id"},
                        "avg_booking_value": {
                            "$avg": {
                                "$cond": [
                                    {"$eq": ["$status", "payment_completed"]},
                                    {"$toInt": {"$arrayElemAt": [{"$split": ["$price", " "]}, 1]}},
                                    None
                                ]
                            }
                        },
                        "last_booking": {"$max": "$created_at"}
                    }
                },
                {
                    "$addFields": {
                        "completion_rate": {
                            "$cond": [
                                {"$gt": ["$total_bookings", 0]},
                                {"$multiply": [{"$divide": ["$completed_bookings", "$total_bookings"]}, 100]},
                                0
                            ]
                        },
                        "unique_guest_count": {"$size": "$unique_guests"}
                    }
                },
                {"$project": {"unique_guests": 0}},  # Remove array field
                {"$sort": {"total_revenue": -1}}
            ]
            
            results = await db["orders"].aggregate(pipeline).to_list(length=None)
            
            # Format results
            for result in results:
                result["villa_code"] = result.pop("_id")
                if result.get("last_booking"):
                    result["last_booking"] = result["last_booking"].isoformat()
                result["avg_booking_value"] = round(result.get("avg_booking_value", 0), 2)
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to get villa performance: {str(e)}")
            return []
    
    async def get_service_analytics(
        self,
        days: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Get analytics for different services
        
        Args:
            days: Number of days to analyze
            
        Returns:
            List of service performance metrics
        """
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            
            pipeline = [
                {"$match": {"created_at": {"$gte": start_date}}},
                {
                    "$group": {
                        "_id": "$service_name",
                        "total_bookings": {"$sum": 1},
                        "completed_bookings": {
                            "$sum": {
                                "$cond": [{"$eq": ["$status", "payment_completed"]}, 1, 0]
                            }
                        },
                        "total_revenue": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$status", "payment_completed"]},
                                    {"$toInt": {"$arrayElemAt": [{"$split": ["$price", " "]}, 1]}},
                                    0
                                ]
                            }
                        },
                        "avg_price": {
                            "$avg": {
                                "$toInt": {"$arrayElemAt": [{"$split": ["$price", " "]}, 1]}
                            }
                        }
                    }
                },
                {
                    "$addFields": {
                        "service_name": "$_id",
                        "completion_rate": {
                            "$cond": [
                                {"$gt": ["$total_bookings", 0]},
                                {"$multiply": [{"$divide": ["$completed_bookings", "$total_bookings"]}, 100]},
                                0
                            ]
                        }
                    }
                },
                {"$project": {"_id": 0}},
                {"$sort": {"total_revenue": -1}}
            ]
            
            results = await db["orders"].aggregate(pipeline).to_list(length=None)
            
            for result in results:
                result["avg_price"] = round(result.get("avg_price", 0), 2)
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to get service analytics: {str(e)}")
            return []
    
    async def get_revenue_analytics(
        self,
        days: int = 30,
        group_by: str = "daily"
    ) -> List[Dict[str, Any]]:
        """
        Get revenue analytics over time
        
        Args:
            days: Number of days to analyze
            group_by: Grouping period (daily, weekly, monthly)
            
        Returns:
            List of revenue data points
        """
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Determine grouping format
            if group_by == "daily":
                date_format = "%Y-%m-%d"
            elif group_by == "weekly":
                date_format = "%Y-W%U"
            else:  # monthly
                date_format = "%Y-%m"
            
            pipeline = [
                {
                    "$match": {
                        "created_at": {"$gte": start_date},
                        "status": "payment_completed"
                    }
                },
                {
                    "$project": {
                        "date": {
                            "$dateToString": {
                                "format": date_format,
                                "date": "$created_at"
                            }
                        },
                        "revenue": {
                            "$toInt": {"$arrayElemAt": [{"$split": ["$price", " "]}, 1]}
                        }
                    }
                },
                {
                    "$group": {
                        "_id": "$date",
                        "revenue": {"$sum": "$revenue"},
                        "bookings": {"$sum": 1}
                    }
                },
                {
                    "$addFields": {
                        "date": "$_id",
                        "avg_booking_value": {"$divide": ["$revenue", "$bookings"]}
                    }
                },
                {"$project": {"_id": 0}},
                {"$sort": {"date": 1}}
            ]
            
            results = await db["orders"].aggregate(pipeline).to_list(length=None)
            
            for result in results:
                result["avg_booking_value"] = round(result.get("avg_booking_value", 0), 2)
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to get revenue analytics: {str(e)}")
            return []
    
    async def get_guest_analytics(
        self,
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Get guest-related analytics
        
        Args:
            days: Number of days to analyze
            
        Returns:
            Guest analytics dictionary
        """
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Total guests and new guests
            pipeline = [
                {"$match": {"created_at": {"$gte": start_date}}},
                {
                    "$group": {
                        "_id": None,
                        "total_guests": {"$addToSet": "$sender_id"},
                        "returning_guests": {
                            "$addToSet": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {
                                                "$size": {
                                                    "$filter": {
                                                        "input": await db["orders"].find({
                                                            "sender_id": "$sender_id",
                                                            "created_at": {"$lt": start_date}
                                                        }).to_list(length=None),
                                                        "cond": {"$expr": True}
                                                    }
                                                }
                                            },
                                            0
                                        ]
                                    },
                                    "$sender_id",
                                    None
                                ]
                            }
                        }
                    }
                },
                {
                    "$addFields": {
                        "total_guest_count": {"$size": "$total_guests"},
                        "new_guest_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$total_guests",
                                    "cond": {"$not": {"$in": ["$$this", "$returning_guests"]}}
                                }
                            }
                        },
                        "returning_guest_count": {"$size": "$returning_guests"}
                    }
                },
                {"$project": {"_id": 0, "total_guests": 0, "returning_guests": 0}}
            ]
            
            result = await db["orders"].aggregate(pipeline).to_list(length=1)
            
            return result[0] if result else {
                "total_guest_count": 0,
                "new_guest_count": 0,
                "returning_guest_count": 0
            }
            
        except Exception as e:
            logger.error(f"Failed to get guest analytics: {str(e)}")
            return {
                "total_guest_count": 0,
                "new_guest_count": 0,
                "returning_guest_count": 0
            }
    
    async def get_promo_analytics(
        self,
        days: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Get promo code usage analytics
        
        Args:
            days: Number of days to analyze
            
        Returns:
            List of promo analytics
        """
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            
            pipeline = [
                {"$match": {"created_at": {"$gte": start_date}}},
                {"$match": {"promo_code": {"$exists": True, "$ne": None}}},
                {
                    "$group": {
                        "_id": "$promo_code",
                        "usage_count": {"$sum": 1},
                        "completed_usage": {
                            "$sum": {
                                "$cond": [{"$eq": ["$status", "payment_completed"]}, 1, 0]
                            }
                        },
                        "total_discount": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$status", "payment_completed"]},
                                    {"$toInt": {"$arrayElemAt": [{"$split": ["$price", " "]}, 1]}},
                                    0
                                ]
                            }
                        }
                    }
                },
                {
                    "$addFields": {
                        "promo_code": "$_id",
                        "conversion_rate": {
                            "$cond": [
                                {"$gt": ["$usage_count", 0]},
                                {"$multiply": [{"$divide": ["$completed_usage", "$usage_count"]}, 100]},
                                0
                            ]
                        }
                    }
                },
                {"$project": {"_id": 0}},
                {"$sort": {"usage_count": -1}}
            ]
            
            results = await db["orders"].aggregate(pipeline).to_list(length=None)
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to get promo analytics: {str(e)}")
            return []

# Global instance
analytics_service = AnalyticsService()

# Convenience functions
async def get_villa_performance(villa_code: Optional[str] = None, days: int = 30):
    return await analytics_service.get_villa_performance(villa_code, days)

async def get_service_analytics(days: int = 30):
    return await analytics_service.get_service_analytics(days)

async def get_revenue_analytics(days: int = 30, group_by: str = "daily"):
    return await analytics_service.get_revenue_analytics(days, group_by)

async def get_guest_analytics(days: int = 30):
    return await analytics_service.get_guest_analytics(days)

async def get_promo_analytics(days: int = 30):
    return await analytics_service.get_promo_analytics(days)
