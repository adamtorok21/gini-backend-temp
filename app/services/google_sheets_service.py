# app/services/google_sheets_service.py
import pandas as pd
from typing import List, Dict, Any, Optional
import logging
from app.services.menu_services import cache, load_data_into_cache
from app.services.google_sheets import get_workbook
from app.settings.config import settings

logger = logging.getLogger(__name__)

class GoogleSheetsService:
    """Service for fetching service data from Google Sheets — synchronized with menu_services cache"""
    
    async def _ensure_cache(self):
        """Log status if cache is empty, but don't block request thread with sync loading"""
        if cache.get("services_df") is None or cache["services_df"].empty:
            logger.warning("⚠️ google_sheets_service: Cache is empty. Data is loading in background...")

    async def get_services_data(self) -> List[Dict[str, Any]]:
        """Fetch all services data from cached dataframes (Services + Archive)"""
        await self._ensure_cache()
        
        services = []
        
        # 1. Process Main Services
        df = cache.get("services_df")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                if row.get('Service Item'):
                    services.append({
                        'service_name': str(row.get('Service Item', '')),
                        'category': str(row.get('Category', '')),
                        'subcategory': str(row.get('Sub-category', '')),
                        'description': str(row.get('Service Item Description', '')),
                        'price': str(row.get('Final Price (Service Item Button)', '')),
                        'duration': str(row.get('Duration', '')),
                        'villa_code': str(row.get('Locations', '')),
                        'service_provider_code': str(row.get('Service Providers', '')),
                        'requirements': str(row.get('Requirements', '')),
                        'availability': str(row.get('Availability', '')),
                        'image_url': str(row.get('Image URL', '')),
                        'tags': str(row.get('Tags', '')).split(',') if row.get('Tags') else []
                    })
        
        # 2. Process Archive (Rentals, etc.)
        arch_df = cache.get("archive_df")
        if arch_df is not None and not arch_df.empty:
            for _, row in arch_df.iterrows():
                # Attempt to map archive columns to service fields
                # Renting often uses 'Service Item' or 'Item' name
                service_name = row.get('Service Item') or row.get('Item') or row.get('Name')
                if service_name and str(service_name).strip():
                    services.append({
                        'service_name': str(service_name),
                        'category': str(row.get('Category', 'Rental')),
                        'subcategory': str(row.get('Sub-category', 'Equipment Rental')),
                        'description': str(row.get('Service Item Description', row.get('Description', ''))),
                        'price': str(row.get('Final Price (Service Item Button)', row.get('Price', '0'))),
                        'duration': str(row.get('Duration', '')),
                        'villa_code': str(row.get('Locations', 'All')),
                        'service_provider_code': str(row.get('Service Providers', 'DEFAULT_SP')),
                        'requirements': str(row.get('Requirements', '')),
                        'availability': str(row.get('Availability', 'Available')),
                        'image_url': str(row.get('Image URL', '')),
                        'tags': ['archive'] + (str(row.get('Tags', '')).split(',') if row.get('Tags') else [])
                    })
                    
        return services
    
    async def get_services_by_category(self, category: str) -> List[Dict[str, Any]]:
        """Get services filtered by category"""
        all_services = await self.get_services_data()
        return [service for service in all_services if service['category'].lower() == category.lower()]
    
    async def get_service_by_name(self, service_name: str) -> Optional[Dict[str, Any]]:
        """Get specific service by name"""
        all_services = await self.get_services_data()
        for service in all_services:
            if service['service_name'].lower() == service_name.lower():
                return service
        return None
    
    async def get_categories(self) -> List[str]:
        """Get all unique categories"""
        all_services = await self.get_services_data()
        categories = list(set(service['category'] for service in all_services if service['category']))
        return sorted([cat for cat in categories if cat])
    
    async def get_subcategories(self, category: str) -> List[str]:
        """Get all subcategories for a category"""
        services = await self.get_services_by_category(category)
        subcategories = list(set(service['subcategory'] for service in services if service['subcategory']))
        return sorted([sub for sub in subcategories if sub])

# Global instance
google_sheets_service = GoogleSheetsService()

# Convenience functions
async def get_all_services():
    return await google_sheets_service.get_services_data()

async def get_services_by_category(category: str):
    return await google_sheets_service.get_services_by_category(category)

async def get_service_by_name(service_name: str):
    return await google_sheets_service.get_service_by_name(service_name)

async def get_categories():
    return await google_sheets_service.get_categories()
