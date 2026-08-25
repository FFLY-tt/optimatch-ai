"""
商机挖掘模块 (Tab A) 接口路由。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.search_agent import suggest_relevant_categories, run_categorized_opportunity_search, LEAD_CATEGORIES
from src.tab_a_outreach.business_profile import build_business_profile, BUSINESS_COLLECTION_NAME
from src.tab_a_outreach.outreach_generator import generate_outreach_message
from src.core.retriever import hybrid_search

router = APIRouter(tags=["Tab A - Business Opportunities"])

# ---------- 1. 建立业务画像 ----------
class SetupBusinessProfileRequest(BaseModel):
    business_description: str = Field(..., description="Business/product description, <=300 chars")
    target_customer: str = Field(..., description="Target customer profile, <=150 chars")
    website_url: str = Field(default="", description="Optional website URL to scrape for extra context")

class SetupBusinessProfileResponse(BaseModel):
    success: bool
    total_chunks: int

@router.post("/api/setup-business-profile", response_model=SetupBusinessProfileResponse)
def setup_business_profile(request: SetupBusinessProfileRequest):
    if not request.business_description.strip() or not request.target_customer.strip():
        raise HTTPException(status_code=400, detail="business_description and target_customer are required")

    chunks = build_business_profile(
        business_description=request.business_description,
        target_customer=request.target_customer,
        website_url=request.website_url,
    )
    return SetupBusinessProfileResponse(success=True, total_chunks=len(chunks))


# ---------- 2. 推荐商机类别 ----------
class SuggestCategoriesRequest(BaseModel):
    business_description: str = Field(..., description="Business description used to infer relevant lead categories")
    max_categories: int = Field(default=3)

class CategoryOption(BaseModel):
    id: str
    label: str
    suggested: bool

class SuggestCategoriesResponse(BaseModel):
    categories: list[CategoryOption]
    suggested_hashtags: list[str] = Field(
        default_factory=list,
        description="LLM 生成的 Instagram hashtag 建议，供 affiliate_kol 类别使用。"
    )

@router.post("/api/suggest-lead-categories", response_model=SuggestCategoriesResponse)
def suggest_lead_categories(request: SuggestCategoriesRequest):
    if not request.business_description.strip():
        raise HTTPException(status_code=400, detail="business_description 不能为空")

    suggested_ids, suggested_hashtags = suggest_relevant_categories(
        request.business_description, request.max_categories
    )
    suggested_ids = set(suggested_ids)

    categories = [
        CategoryOption(id=cat_id, label=info["label"], suggested=(cat_id in suggested_ids))
        for cat_id, info in LEAD_CATEGORIES.items()
    ]
    return SuggestCategoriesResponse(categories=categories, suggested_hashtags=suggested_hashtags)


# ---------- 3. 执行商机搜索 ----------
class OpportunityRecord(BaseModel):
    id: str
    source: str
    title: str
    url: str
    posted_at: str | None
    status: str = "new"
    category: str
    email: str | None = None
    followers: int | None = None

class SearchOpportunitiesRequest(BaseModel):
    categories: list[str] = Field(..., description="Which lead categories to search")
    hashtags: list[str] = Field(default_factory=list, description="Instagram hashtags to use")
    max_results_per_category: int = Field(default=5)

class SearchOpportunitiesResponse(BaseModel):
    opportunities: list[OpportunityRecord]
    total: int

@router.post("/api/search-opportunities", response_model=SearchOpportunitiesResponse)
def search_opportunities(request: SearchOpportunitiesRequest):
    try:
        matches = hybrid_search(
            "business description", collection_name=BUSINESS_COLLECTION_NAME, final_top_k=5
        )
        context = "\n".join(m["content"] for m in matches)
    except RuntimeError:
        raise HTTPException(
            status_code=400,
            detail="No business profile found. Call /api/setup-business-profile first."
        )

    if not request.categories:
        raise HTTPException(status_code=400, detail="categories 不能为空，至少选择一个线索类别")

    try:
        categorized = run_categorized_opportunity_search(
            business_context=context,
            categories=request.categories,
            hashtags=request.hashtags,
            max_results_per_category=request.max_results_per_category,
            concurrent=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Categorized search failed: {e}")

    opportunities = []
    for cat_id, records in categorized.items():
        for r in records:
            opportunities.append(OpportunityRecord(
                id=f"{r.source}_{abs(hash(r.url))}",
                source=r.source, title=r.title, url=r.url,
                posted_at=r.posted_at, status="new", category=cat_id,
                email=r.email, followers=r.followers,
            ))

    return SearchOpportunitiesResponse(opportunities=opportunities, total=len(opportunities))


# ---------- 4. 生成开发信 ----------
class GenerateOutreachRequest(BaseModel):
    opportunity_content: str = Field(..., description="Full text of the target opportunity")
    user_notes: str = Field(default="", description="Optional context to clarify vague business info")

class GenerateOutreachResponse(BaseModel):
    outreach_message: str
    passed_review: bool
    issue: str
    attempts: int
    matched_sections: list[str]

@router.post("/api/generate-outreach", response_model=GenerateOutreachResponse)
def generate_outreach(request: GenerateOutreachRequest):
    if not request.opportunity_content.strip():
        raise HTTPException(status_code=400, detail="opportunity_content 不能为空")

    try:
        matches = hybrid_search(
            request.opportunity_content, collection_name=BUSINESS_COLLECTION_NAME, final_top_k=3
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Retrieval failed: {e}. Call /api/setup-business-profile first."
        )

    result = generate_outreach_message(
        business_chunks=matches,
        opportunity_content=request.opportunity_content,
        user_notes=request.user_notes,
    )

    return GenerateOutreachResponse(
        outreach_message=result["outreach_message"],
        passed_review=result["passed_review"],
        issue=result["issue"],
        attempts=result["attempts"],
        matched_sections=[m["section"] for m in matches],
    )