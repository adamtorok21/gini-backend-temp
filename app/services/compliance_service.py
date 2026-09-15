import logging
from datetime import datetime
from typing import Optional, Dict, Any
from app.db.session import db

logger = logging.getLogger(__name__)

class ComplianceService:
    """Service for logging compliance-related activities"""
    
    COMPLIANCE_LOG_COLLECTION = "compliance_logs"
    
    async def log_pii_access(
        self,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        """
        Log access to Personally Identifiable Information (PII)
        
        Args:
            user_id: ID of the user accessing the data
            action: Action performed (read, write, delete, etc.)
            resource_type: Type of resource accessed (passport, order, etc.)
            resource_id: ID of the specific resource
            ip_address: Client IP address
            user_agent: Client user agent string
        """
        try:
            log_entry = {
                "timestamp": datetime.utcnow(),
                "user_id": user_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "ip_address": ip_address,
                "user_agent": user_agent,
                "compliance_type": "pii_access",
                "sensitive": True
            }
            
            await db[self.COMPLIANCE_LOG_COLLECTION].insert_one(log_entry)
            logger.info(f"PII access logged: {user_id} -> {action} on {resource_type}")
            
        except Exception as e:
            logger.error(f"Failed to log PII access: {str(e)}")
    
    async def log_data_export(
        self,
        user_id: str,
        export_type: str,
        record_count: int,
        filters: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ):
        """
        Log data export activities
        
        Args:
            user_id: ID of the user performing export
            export_type: Type of data exported (orders, guests, etc.)
            record_count: Number of records exported
            filters: Filters applied to the export
            ip_address: Client IP address
        """
        try:
            log_entry = {
                "timestamp": datetime.utcnow(),
                "user_id": user_id,
                "action": "data_export",
                "export_type": export_type,
                "record_count": record_count,
                "filters": filters or {},
                "ip_address": ip_address,
                "compliance_type": "data_export",
                "sensitive": True
            }
            
            await db[self.COMPLIANCE_LOG_COLLECTION].insert_one(log_entry)
            logger.info(f"Data export logged: {user_id} exported {record_count} {export_type}")
            
        except Exception as e:
            logger.error(f"Failed to log data export: {str(e)}")
    
    async def log_admin_action(
        self,
        user_id: str,
        action: str,
        target_resource: str,
        target_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ):
        """
        Log administrative actions
        
        Args:
            user_id: ID of the admin user
            action: Action performed (delete, modify, access, etc.)
            target_resource: Resource being acted upon
            target_id: ID of the target resource
            details: Additional details about the action
            ip_address: Client IP address
        """
        try:
            log_entry = {
                "timestamp": datetime.utcnow(),
                "user_id": user_id,
                "action": action,
                "target_resource": target_resource,
                "target_id": target_id,
                "details": details or {},
                "ip_address": ip_address,
                "compliance_type": "admin_action",
                "sensitive": True
            }
            
            await db[self.COMPLIANCE_LOG_COLLECTION].insert_one(log_entry)
            logger.info(f"Admin action logged: {user_id} -> {action} on {target_resource}")
            
        except Exception as e:
            logger.error(f"Failed to log admin action: {str(e)}")
    
    async def log_security_event(
        self,
        event_type: str,
        severity: str,
        description: str,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        """
        Log security-related events
        
        Args:
            event_type: Type of security event (login_failed, suspicious_activity, etc.)
            severity: Severity level (low, medium, high, critical)
            description: Event description
            user_id: Related user ID if applicable
            ip_address: Client IP address
            details: Additional event details
        """
        try:
            log_entry = {
                "timestamp": datetime.utcnow(),
                "event_type": event_type,
                "severity": severity,
                "description": description,
                "user_id": user_id,
                "ip_address": ip_address,
                "details": details or {},
                "compliance_type": "security_event",
                "sensitive": True
            }
            
            await db[self.COMPLIANCE_LOG_COLLECTION].insert_one(log_entry)
            logger.warning(f"Security event logged: {event_type} - {description}")
            
        except Exception as e:
            logger.error(f"Failed to log security event: {str(e)}")
    
    async def get_compliance_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        compliance_type: Optional[str] = None,
        user_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ):
        """
        Retrieve compliance logs with filtering
        
        Args:
            limit: Maximum number of logs to return
            offset: Number of logs to skip
            compliance_type: Filter by compliance type
            user_id: Filter by user ID
            start_date: Filter logs after this date
            end_date: Filter logs before this date
            
        Returns:
            List of compliance log entries
        """
        try:
            # Build query filter
            query = {}
            
            if compliance_type:
                query["compliance_type"] = compliance_type
            
            if user_id:
                query["user_id"] = user_id
            
            if start_date or end_date:
                timestamp_query = {}
                if start_date:
                    timestamp_query["$gte"] = start_date
                if end_date:
                    timestamp_query["$lte"] = end_date
                query["timestamp"] = timestamp_query
            
            # Execute query with pagination
            cursor = db[self.COMPLIANCE_LOG_COLLECTION].find(query).sort("timestamp", -1).skip(offset).limit(limit)
            logs = await cursor.to_list(length=limit)
            
            # Convert ObjectId to string for JSON serialization
            for log in logs:
                log["_id"] = str(log["_id"])
                if "timestamp" in log:
                    log["timestamp"] = log["timestamp"].isoformat()
            
            return logs
            
        except Exception as e:
            logger.error(f"Failed to retrieve compliance logs: {str(e)}")
            return []
    
    async def get_compliance_stats(self) -> Dict[str, Any]:
        """
        Get compliance statistics for dashboard
        
        Returns:
            Dictionary with compliance statistics
        """
        try:
            pipeline = [
                {
                    "$group": {
                        "_id": "$compliance_type",
                        "count": {"$sum": 1},
                        "last_occurrence": {"$max": "$timestamp"}
                    }
                },
                {"$sort": {"count": -1}}
            ]
            
            stats = await db[self.COMPLIANCE_LOG_COLLECTION].aggregate(pipeline).to_list(length=None)
            
            # Format stats
            formatted_stats = {}
            for stat in stats:
                formatted_stats[stat["_id"]] = {
                    "count": stat["count"],
                    "last_occurrence": stat["last_occurrence"].isoformat() if stat.get("last_occurrence") else None
                }
            
            return formatted_stats
            
        except Exception as e:
            logger.error(f"Failed to get compliance stats: {str(e)}")
            return {}

# Global instance
compliance_service = ComplianceService()

# Convenience functions
async def log_pii_access(user_id: str, action: str, resource_type: str, **kwargs):
    await compliance_service.log_pii_access(user_id, action, resource_type, **kwargs)

async def log_data_export(user_id: str, export_type: str, record_count: int, **kwargs):
    await compliance_service.log_data_export(user_id, export_type, record_count, **kwargs)

async def log_admin_action(user_id: str, action: str, target_resource: str, **kwargs):
    await compliance_service.log_admin_action(user_id, action, target_resource, **kwargs)

async def log_security_event(event_type: str, severity: str, description: str, **kwargs):
    await compliance_service.log_security_event(event_type, severity, description, **kwargs)
