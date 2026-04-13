from collections import defaultdict
from datetime import date, timedelta

import boto3
import pandas as pd
import streamlit as st
from dateutil.relativedelta import relativedelta

STACK_TAG_KEY = "aws:cloudformation:stack-name"
UNTAGGED_LABEL = "(untagged)"
MONTH_COUNT = 6
ACTIVE_STACK_STATUSES = [
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
]
RESOURCE_FAMILY_OVERRIDES = {
    "ApiGateway": "API Gateway",
    "ApplicationAutoScaling": "Application Auto Scaling",
    "AutoScaling": "Auto Scaling",
    "CertificateManager": "Certificate Manager",
    "CloudFormation": "CloudFormation",
    "CloudFront": "CloudFront",
    "CloudWatch": "CloudWatch",
    "CodeBuild": "CodeBuild",
    "CodePipeline": "CodePipeline",
    "Cognito": "Cognito",
    "DynamoDB": "DynamoDB",
    "EC2": "EC2",
    "ECR": "ECR",
    "ECS": "ECS",
    "EFS": "EFS",
    "EKS": "EKS",
    "ElasticLoadBalancing": "Elastic Load Balancing",
    "ElasticLoadBalancingV2": "Elastic Load Balancing",
    "Events": "EventBridge",
    "IAM": "IAM",
    "KMS": "KMS",
    "Lambda": "Lambda",
    "Logs": "CloudWatch Logs",
    "OpenSearchService": "OpenSearch",
    "RDS": "RDS",
    "Route53": "Route 53",
    "S3": "S3",
    "SNS": "SNS",
    "SQS": "SQS",
    "SSM": "Systems Manager",
    "Scheduler": "EventBridge Scheduler",
    "SecretsManager": "Secrets Manager",
    "StepFunctions": "Step Functions",
}
RESOURCE_TO_BILLING_SERVICE = {
    "API Gateway": "Amazon API Gateway",
    "CloudFormation": "AWS CloudFormation",
    "CloudFront": "Amazon CloudFront",
    "CloudWatch": "AmazonCloudWatch",
    "CloudWatch Logs": "AmazonCloudWatch",
    "CodeBuild": "AWS CodeBuild",
    "CodePipeline": "AWS CodePipeline",
    "Cognito": "Amazon Cognito",
    "DynamoDB": "Amazon DynamoDB",
    "EC2": "Amazon Elastic Compute Cloud - Compute",
    "ECR": "Amazon EC2 Container Registry (ECR)",
    "ECS": "Amazon Elastic Container Service",
    "EFS": "Amazon Elastic File System",
    "EKS": "Amazon Elastic Kubernetes Service",
    "Elastic Load Balancing": "Amazon Elastic Load Balancing",
    "EventBridge": "Amazon EventBridge",
    "EventBridge Scheduler": "Amazon EventBridge",
    "KMS": "AWS Key Management Service",
    "Lambda": "AWS Lambda",
    "OpenSearch": "Amazon OpenSearch Service",
    "RDS": "Amazon Relational Database Service",
    "Route 53": "Amazon Route 53",
    "S3": "Amazon Simple Storage Service",
    "SNS": "Amazon Simple Notification Service",
    "SQS": "Amazon Simple Queue Service",
    "Secrets Manager": "AWS Secrets Manager",
    "Step Functions": "AWS Step Functions",
    "Systems Manager": "AWS Systems Manager",
}
HEURISTIC_SERVICE_MATCHERS = {
    "Amazon Elastic Load Balancing": {
        "basis": "ALB / ELB resources discovered in CloudFormation",
        "resource_types": {
            "AWS::ElasticLoadBalancing::LoadBalancer",
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "AWS::ElasticLoadBalancingV2::Listener",
            "AWS::ElasticLoadBalancingV2::ListenerRule",
            "AWS::ElasticLoadBalancingV2::TargetGroup",
        },
    },
    "Amazon Route 53": {
        "basis": "Route 53 records or hosted zone resources discovered in CloudFormation",
        "resource_types": {
            "AWS::Route53::HostedZone",
            "AWS::Route53::HealthCheck",
            "AWS::Route53::RecordSet",
            "AWS::Route53::RecordSetGroup",
        },
    },
    "Amazon Virtual Private Cloud": {
        "basis": "VPC resources discovered in CloudFormation",
        "resource_types": {
            "AWS::EC2::CarrierGateway",
            "AWS::EC2::CustomerGateway",
            "AWS::EC2::EIP",
            "AWS::EC2::EgressOnlyInternetGateway",
            "AWS::EC2::FlowLog",
            "AWS::EC2::InternetGateway",
            "AWS::EC2::NatGateway",
            "AWS::EC2::NetworkAcl",
            "AWS::EC2::NetworkInterface",
            "AWS::EC2::Route",
            "AWS::EC2::RouteTable",
            "AWS::EC2::SecurityGroup",
            "AWS::EC2::Subnet",
            "AWS::EC2::SubnetCidrBlock",
            "AWS::EC2::TransitGateway",
            "AWS::EC2::TransitGatewayAttachment",
            "AWS::EC2::TransitGatewayRouteTable",
            "AWS::EC2::VPCCidrBlock",
            "AWS::EC2::VPCEndpoint",
            "AWS::EC2::VPC",
            "AWS::EC2::VPCGatewayAttachment",
            "AWS::EC2::VPCPeeringConnection",
        },
    },
}


def make_client(service: str, profile: str, region: str):
    session_kwargs = {}
    if profile.strip():
        session_kwargs["profile_name"] = profile.strip()
    session = boto3.Session(**session_kwargs)
    client_region = "us-east-1" if service == "ce" else region
    return session.client(service, region_name=client_region)


@st.cache_data(ttl=900, show_spinner=False)
def get_available_profiles():
    return boto3.session.Session().available_profiles


def get_month_windows(today: date, months: int):
    windows = []
    current_month_start = today.replace(day=1)

    for offset in reversed(range(months)):
        month_start = current_month_start - relativedelta(months=offset)
        month_end = month_start + relativedelta(months=1)
        if offset == 0:
            month_end = min(today + timedelta(days=1), month_end)

        label = month_start.strftime("%b %Y")
        if offset == 0:
            label = f"{label} (MTD)"

        windows.append(
            {
                "key": month_start.strftime("%Y-%m"),
                "label": label,
                "short_label": month_start.strftime("%b %Y"),
                "start": month_start.strftime("%Y-%m-%d"),
                "end": month_end.strftime("%Y-%m-%d"),
            }
        )

    return windows


def parse_stack_tag_value(raw_key: str):
    if not raw_key:
        return UNTAGGED_LABEL

    if "$" in raw_key:
        raw_key = raw_key.split("$", 1)[1]

    raw_key = raw_key.strip()
    if not raw_key or raw_key.lower().startswith("no tagkey"):
        return UNTAGGED_LABEL
    return raw_key


def get_resource_family(resource_type: str):
    parts = resource_type.split("::")
    if len(parts) >= 2 and parts[0] == "AWS":
        return RESOURCE_FAMILY_OVERRIDES.get(parts[1], parts[1])
    return resource_type


def format_currency(value: float):
    return f"${value:,.2f}"


def format_currency_plain(value: float):
    if value == 0:
        return "USD 0.00"
    if 0 < abs(value) < 0.01:
        return "USD <$0.01"
    return f"USD {value:,.2f}"


def format_delta(value: float):
    return f"{value:+,.2f}"


def format_cost_display(value):
    if pd.isna(value):
        return ""

    numeric_value = float(value)
    if numeric_value == 0:
        return "$0.00"
    if 0 < abs(numeric_value) < 0.01:
        return "<$0.01"
    return f"${numeric_value:,.2f}"


def get_month_key_from_period(period):
    return period["TimePeriod"]["Start"][:7]


def round_cost_mapping(cost_mapping):
    return {
        owner: {
            month_key: round(amount, 2)
            for month_key, amount in monthly_costs.items()
            if round(amount, 2) > 0
        }
        for owner, monthly_costs in cost_mapping.items()
    }


def round_cost_mapping_by_service(cost_mapping):
    rounded = {}
    for owner, services in cost_mapping.items():
        rounded[owner] = {}
        for service_name, monthly_costs in services.items():
            monthly = {
                month_key: round(amount, 2)
                for month_key, amount in monthly_costs.items()
                if round(amount, 2) > 0
            }
            if monthly:
                rounded[owner][service_name] = monthly
    return rounded


def apply_cost_display(df: pd.DataFrame, cost_columns):
    display_df = df.copy()
    for column in cost_columns:
        if column in display_df.columns:
            display_df[column] = display_df[column].apply(format_cost_display)
    return display_df


def summarize_matched_resources(resources, max_items=3):
    if not resources:
        return ""

    summary_parts = []
    for resource in resources[:max_items]:
        resource_name = resource["Type"].split("::")[-1]
        summary_parts.append(f"{resource['Logical ID']} ({resource_name})")

    if len(resources) > max_items:
        summary_parts.append(f"+{len(resources) - max_items} more")

    return ", ".join(summary_parts)


def format_candidate_shares(candidates, max_items=3):
    if not candidates:
        return ""

    total_weight = sum(candidate["weight"] for candidate in candidates)
    parts = []
    for candidate in candidates[:max_items]:
        share_pct = 100 * candidate["weight"] / total_weight if total_weight else 0
        parts.append(f"{candidate['stack_name']} ({share_pct:.0f}%)")

    if len(candidates) > max_items:
        parts.append(f"+{len(candidates) - max_items} more")

    return ", ".join(parts)


def determine_attribution_mode(exact_total: float, heuristic_total: float):
    if exact_total > 0 and heuristic_total > 0:
        return "Mixed"
    if exact_total > 0:
        return "Exact only"
    if heuristic_total > 0:
        return "Heuristic only"
    return "No cost"


def normalize_dns_name(name: str):
    return name.rstrip(".").lower()


def fetch_cost_pages(profile: str, region: str, start: str, end: str, group_by=None):
    ce = make_client("ce", profile, region)
    params = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
    }
    if group_by:
        params["GroupBy"] = group_by

    pages = []
    next_page_token = None

    while True:
        request = dict(params)
        if next_page_token:
            request["NextPageToken"] = next_page_token
        response = ce.get_cost_and_usage(**request)
        pages.append(response)
        next_page_token = response.get("NextPageToken")
        if not next_page_token:
            break

    return pages


@st.cache_data(ttl=900, show_spinner=False)
def get_total_costs_by_month(profile: str, region: str, start: str, end: str):
    totals = defaultdict(float)
    for page in fetch_cost_pages(profile, region, start, end):
        for period in page["ResultsByTime"]:
            month_key = get_month_key_from_period(period)
            totals[month_key] += float(period["Total"]["UnblendedCost"]["Amount"])
    return {month_key: round(value, 2) for month_key, value in totals.items()}


@st.cache_data(ttl=900, show_spinner=False)
def get_costs_by_stack_by_month(profile: str, region: str, start: str, end: str):
    nested_totals = defaultdict(lambda: defaultdict(float))
    pages = fetch_cost_pages(
        profile,
        region,
        start,
        end,
        group_by=[{"Type": "TAG", "Key": STACK_TAG_KEY}],
    )

    for page in pages:
        for period in page["ResultsByTime"]:
            month_key = get_month_key_from_period(period)
            for group in period["Groups"]:
                stack_name = parse_stack_tag_value(group["Keys"][0])
                nested_totals[stack_name][month_key] += float(
                    group["Metrics"]["UnblendedCost"]["Amount"]
                )

    return {
        stack_name: {
            month_key: round(cost, 2)
            for month_key, cost in month_costs.items()
            if round(cost, 2) > 0
        }
        for stack_name, month_costs in nested_totals.items()
    }


@st.cache_data(ttl=900, show_spinner=False)
def get_costs_by_stack_and_service_by_month(profile: str, region: str, start: str, end: str):
    nested_totals = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    pages = fetch_cost_pages(
        profile,
        region,
        start,
        end,
        group_by=[
            {"Type": "TAG", "Key": STACK_TAG_KEY},
            {"Type": "DIMENSION", "Key": "SERVICE"},
        ],
    )

    for page in pages:
        for period in page["ResultsByTime"]:
            month_key = get_month_key_from_period(period)
            for group in period["Groups"]:
                stack_name = parse_stack_tag_value(group["Keys"][0])
                billing_service = group["Keys"][1]
                nested_totals[stack_name][billing_service][month_key] += float(
                    group["Metrics"]["UnblendedCost"]["Amount"]
                )

    cleaned = {}
    for stack_name, services in nested_totals.items():
        cleaned[stack_name] = {}
        for billing_service, month_costs in services.items():
            rounded = {
                month_key: round(cost, 2)
                for month_key, cost in month_costs.items()
                if round(cost, 2) > 0
            }
            if rounded:
                cleaned[stack_name][billing_service] = rounded
    return cleaned


def get_stacks(profile: str, region: str):
    cf = make_client("cloudformation", profile, region)
    paginator = cf.get_paginator("list_stacks")
    stacks = []
    for page in paginator.paginate(StackStatusFilter=ACTIVE_STACK_STATUSES):
        stacks.extend(page["StackSummaries"])
    return sorted(stacks, key=lambda stack: stack["StackName"].lower())


def get_stack_resources(profile: str, region: str, stack_name: str):
    cf = make_client("cloudformation", profile, region)
    paginator = cf.get_paginator("list_stack_resources")
    resources = []
    for page in paginator.paginate(StackName=stack_name):
        resources.extend(page["StackResourceSummaries"])
    return resources


@st.cache_data(ttl=900, show_spinner=False)
def load_stack_inventory(profile: str, region: str):
    stack_inventory = {}
    for stack in get_stacks(profile, region):
        stack_name = stack["StackName"]
        resources = get_stack_resources(profile, region, stack_name)

        resources_rows = []
        service_summary = defaultdict(lambda: {"Resource Count": 0, "Resource Types": set()})
        for resource in resources:
            service_name = get_resource_family(resource["ResourceType"])
            resources_rows.append(
                {
                    "Service": service_name,
                    "Logical ID": resource["LogicalResourceId"],
                    "Type": resource["ResourceType"],
                    "Status": resource["ResourceStatus"],
                    "Physical ID": resource.get("PhysicalResourceId", ""),
                }
            )
            service_summary[service_name]["Resource Count"] += 1
            service_summary[service_name]["Resource Types"].add(resource["ResourceType"])

        service_rows = []
        for service_name, summary in sorted(service_summary.items()):
            service_rows.append(
                {
                    "Service": service_name,
                    "Resource Count": summary["Resource Count"],
                    "Resource Types": ", ".join(sorted(summary["Resource Types"])),
                }
            )

        stack_inventory[stack_name] = {
            "stack_name": stack_name,
            "resource_count": len(resources_rows),
            "resource_rows": resources_rows,
            "service_rows": service_rows,
            "service_count": len(service_rows),
        }

    return stack_inventory


@st.cache_data(ttl=900, show_spinner=False)
def get_hosted_zone_record_counts(profile: str):
    session_kwargs = {}
    if profile.strip():
        session_kwargs["profile_name"] = profile.strip()
    session = boto3.Session(**session_kwargs)
    route53 = session.client("route53")

    hosted_zones = []
    for page in route53.get_paginator("list_hosted_zones").paginate():
        for zone in page["HostedZones"]:
            hosted_zones.append(
                {
                    "name": normalize_dns_name(zone["Name"]),
                    "record_count": zone.get("ResourceRecordSetCount", 0),
                }
            )

    hosted_zones.sort(key=lambda zone: len(zone["name"]), reverse=True)
    return hosted_zones


def get_hosted_zone_for_record(record_name: str, hosted_zones):
    normalized_record_name = normalize_dns_name(record_name)
    for zone in hosted_zones:
        zone_name = zone["name"]
        if normalized_record_name == zone_name or normalized_record_name.endswith(f".{zone_name}"):
            return zone
    return None


def get_heuristic_candidates(stack_inventory, billing_service: str, hosted_zones):
    matcher = HEURISTIC_SERVICE_MATCHERS.get(billing_service)
    if not matcher:
        return [], "No heuristic configured for this billing service", "No heuristic configured", {}

    candidates = []
    excluded_candidates = {}
    for stack_name, inventory in stack_inventory.items():
        matched_resources = [
            resource
            for resource in inventory["resource_rows"]
            if resource["Type"] in matcher["resource_types"]
        ]
        if matched_resources:
            if billing_service == "Amazon Route 53":
                zone_resource_counts = defaultdict(int)
                shared_zone_messages = []
                for resource in matched_resources:
                    zone = get_hosted_zone_for_record(resource["Physical ID"], hosted_zones)
                    if zone:
                        zone_resource_counts[zone["name"]] += 1

                for zone_name, stack_record_count in zone_resource_counts.items():
                    zone_record_count = next(
                        zone["record_count"] for zone in hosted_zones if zone["name"] == zone_name
                    )
                    if zone_record_count > stack_record_count:
                        shared_zone_messages.append(
                            f"{zone_name} has {zone_record_count} records; stack manages {stack_record_count}"
                        )

                if shared_zone_messages:
                    excluded_candidates[stack_name] = {
                        "status": "Shared hosted zone - unattributed",
                        "basis": matcher["basis"],
                        "evidence": "; ".join(shared_zone_messages),
                    }
                    continue

            candidates.append(
                {
                    "stack_name": stack_name,
                    "weight": len(matched_resources),
                    "match_count": len(matched_resources),
                    "evidence": summarize_matched_resources(matched_resources),
                }
            )

    candidates.sort(key=lambda candidate: (-candidate["weight"], candidate["stack_name"].lower()))
    if candidates:
        status = "Weighted allocation"
    elif excluded_candidates:
        status = "Shared hosted zone - unattributed"
    else:
        status = "No stack match"
    return candidates, matcher["basis"], status, excluded_candidates


def build_heuristic_allocations(stack_inventory, unattributed_service_costs_by_month, month_windows, hosted_zones):
    stack_monthly_estimates = defaultdict(lambda: defaultdict(float))
    stack_service_estimates = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    service_hint_rows = []
    stack_service_metadata = defaultdict(dict)
    unresolved_monthly = defaultdict(float)

    for billing_service, monthly_costs in sorted(unattributed_service_costs_by_month.items()):
        candidates, basis, heuristic_status, excluded_candidates = get_heuristic_candidates(
            stack_inventory, billing_service, hosted_zones
        )
        total_weight = sum(candidate["weight"] for candidate in candidates)

        hint_row = {
            "Billing Service": billing_service,
            "Likely Stacks": format_candidate_shares(candidates),
            "Heuristic Basis": basis,
            "Heuristic Status": heuristic_status,
        }

        total_cost = 0.0
        for window in month_windows:
            value = monthly_costs.get(window["key"], 0.0)
            hint_row[window["label"]] = value
            total_cost += value

        hint_row["Total ($)"] = round(total_cost, 2)
        service_hint_rows.append(hint_row)

        for stack_name, excluded_metadata in excluded_candidates.items():
            stack_service_metadata[stack_name][billing_service] = excluded_metadata

        if not candidates or total_weight == 0:
            for month_key, value in monthly_costs.items():
                unresolved_monthly[month_key] += value
            continue

        for candidate in candidates:
            share_pct = candidate["weight"] / total_weight
            stack_service_metadata[candidate["stack_name"]][billing_service] = {
                "status": heuristic_status,
                "basis": basis,
                "evidence": candidate["evidence"],
                "match_count": candidate["match_count"],
                "candidate_count": len(candidates),
                "share_pct": share_pct,
            }

            for month_key, value in monthly_costs.items():
                share_value = value * share_pct
                stack_monthly_estimates[candidate["stack_name"]][month_key] += share_value
                stack_service_estimates[candidate["stack_name"]][billing_service][month_key] += share_value

    return (
        round_cost_mapping(stack_monthly_estimates),
        round_cost_mapping_by_service(stack_service_estimates),
        service_hint_rows,
        stack_service_metadata,
        {month_key: round(value, 2) for month_key, value in unresolved_monthly.items() if round(value, 2) > 0},
    )


def build_stack_rows(stack_inventory, exact_stack_costs_by_month, heuristic_stack_costs_by_month, month_windows):
    all_stack_names = sorted(
        (
            set(stack_inventory.keys())
            | {stack_name for stack_name in exact_stack_costs_by_month.keys() if stack_name != UNTAGGED_LABEL}
            | set(heuristic_stack_costs_by_month.keys())
        ),
        key=str.lower,
    )

    rows = []
    for stack_name in all_stack_names:
        inventory = stack_inventory.get(
            stack_name,
            {"resource_count": 0, "service_count": 0},
        )
        row = {
            "Stack": stack_name,
            "Services": inventory["service_count"],
            "Resources": inventory["resource_count"],
            "In CloudFormation": "Yes" if stack_name in stack_inventory else "No",
        }

        total = 0.0
        exact_total = 0.0
        heuristic_total = 0.0
        exact_monthly_costs = exact_stack_costs_by_month.get(stack_name, {})
        heuristic_monthly_costs = heuristic_stack_costs_by_month.get(stack_name, {})
        for window in month_windows:
            exact_value = exact_monthly_costs.get(window["key"], 0.0)
            heuristic_value = heuristic_monthly_costs.get(window["key"], 0.0)
            value = round(exact_value + heuristic_value, 2)
            row[window["label"]] = value
            total += value
            exact_total += exact_value
            heuristic_total += heuristic_value

        row["Total ($)"] = round(total, 2)
        row["Exact Tagged ($)"] = round(exact_total, 2)
        row["Heuristic ($)"] = round(heuristic_total, 2)
        row["Attribution"] = determine_attribution_mode(row["Exact Tagged ($)"], row["Heuristic ($)"])
        rows.append(row)

    return rows


def build_service_cost_rows(inventory_services, service_costs_by_month, month_windows):
    inventory_lookup = {row["Service"]: row for row in inventory_services}
    billing_service_lookup = {
        service_name: RESOURCE_TO_BILLING_SERVICE.get(service_name)
        for service_name in inventory_lookup
    }

    rows = []
    used_billing_services = set()

    for service_name, inventory_row in sorted(inventory_lookup.items()):
        billing_service = billing_service_lookup.get(service_name)
        row = {
            "Service": service_name,
            "Billing Service": billing_service or "",
            "Resources": inventory_row["Resource Count"],
        }

        total = 0.0
        monthly_costs = service_costs_by_month.get(billing_service, {}) if billing_service else {}
        if billing_service:
            used_billing_services.add(billing_service)

        for window in month_windows:
            value = monthly_costs.get(window["key"], 0.0)
            row[window["label"]] = value
            total += value

        row["Total ($)"] = round(total, 2)
        rows.append(row)

    extra_billing_services = sorted(set(service_costs_by_month) - used_billing_services)
    for billing_service in extra_billing_services:
        row = {
            "Service": "Billing-only",
            "Billing Service": billing_service,
            "Resources": 0,
        }
        total = 0.0
        monthly_costs = service_costs_by_month[billing_service]
        for window in month_windows:
            value = monthly_costs.get(window["key"], 0.0)
            row[window["label"]] = value
            total += value

        row["Total ($)"] = round(total, 2)
        rows.append(row)

    return rows


def build_heuristic_service_rows(service_costs_by_month, service_metadata, month_windows):
    rows = []
    for billing_service, monthly_costs in sorted(service_costs_by_month.items()):
        metadata = service_metadata.get(billing_service, {})
        row = {
            "Billing Service": billing_service,
            "Likely Share": f"{metadata.get('share_pct', 0) * 100:.0f}%",
            "Heuristic Status": metadata.get("status", ""),
            "Heuristic Basis": metadata.get("basis", ""),
            "Evidence": metadata.get("evidence", ""),
        }

        total = 0.0
        for window in month_windows:
            value = monthly_costs.get(window["key"], 0.0)
            row[window["label"]] = value
            total += value

        row["Total ($)"] = round(total, 2)
        rows.append(row)

    return rows


def build_combined_service_rows(
    inventory_services,
    exact_service_costs_by_month,
    heuristic_service_costs_by_month,
    heuristic_service_metadata,
    month_windows,
):
    inventory_lookup = {row["Service"]: row for row in inventory_services}
    billing_service_lookup = {
        service_name: RESOURCE_TO_BILLING_SERVICE.get(service_name)
        for service_name in inventory_lookup
    }

    rows = []
    used_billing_services = set()

    for service_name, inventory_row in sorted(inventory_lookup.items()):
        billing_service = billing_service_lookup.get(service_name)
        exact_monthly_costs = exact_service_costs_by_month.get(billing_service, {}) if billing_service else {}
        heuristic_monthly_costs = (
            heuristic_service_costs_by_month.get(billing_service, {}) if billing_service else {}
        )
        heuristic_metadata = heuristic_service_metadata.get(billing_service, {}) if billing_service else {}
        if billing_service:
            used_billing_services.add(billing_service)

        exact_total = round(sum(exact_monthly_costs.values()), 2)
        heuristic_total = round(sum(heuristic_monthly_costs.values()), 2)
        row = {
            "Service": service_name,
            "Billing Service": billing_service or "",
            "Resources": inventory_row["Resource Count"],
            "Exact Tagged ($)": exact_total,
            "Heuristic ($)": heuristic_total,
            "Blended Total ($)": round(exact_total + heuristic_total, 2),
            "Attribution": determine_attribution_mode(exact_total, heuristic_total),
            "Heuristic Status": heuristic_metadata.get("status", ""),
            "Heuristic Basis": heuristic_metadata.get("basis", ""),
        }

        for window in month_windows:
            value = round(
                exact_monthly_costs.get(window["key"], 0.0)
                + heuristic_monthly_costs.get(window["key"], 0.0),
                2,
            )
            row[window["label"]] = value

        rows.append(row)

    extra_billing_services = sorted(
        (set(exact_service_costs_by_month) | set(heuristic_service_costs_by_month)) - used_billing_services
    )
    for billing_service in extra_billing_services:
        exact_monthly_costs = exact_service_costs_by_month.get(billing_service, {})
        heuristic_monthly_costs = heuristic_service_costs_by_month.get(billing_service, {})
        heuristic_metadata = heuristic_service_metadata.get(billing_service, {})
        exact_total = round(sum(exact_monthly_costs.values()), 2)
        heuristic_total = round(sum(heuristic_monthly_costs.values()), 2)

        row = {
            "Service": "Billing-only",
            "Billing Service": billing_service,
            "Resources": 0,
            "Exact Tagged ($)": exact_total,
            "Heuristic ($)": heuristic_total,
            "Blended Total ($)": round(exact_total + heuristic_total, 2),
            "Attribution": determine_attribution_mode(exact_total, heuristic_total),
            "Heuristic Status": heuristic_metadata.get("status", ""),
            "Heuristic Basis": heuristic_metadata.get("basis", ""),
        }

        for window in month_windows:
            value = round(
                exact_monthly_costs.get(window["key"], 0.0)
                + heuristic_monthly_costs.get(window["key"], 0.0),
                2,
            )
            row[window["label"]] = value

        rows.append(row)

    return rows


def add_total_row(df: pd.DataFrame, label_column: str, label_value: str, cost_columns, sum_columns):
    if df.empty:
        return df

    summary_row = {column: "" for column in df.columns}
    summary_row[label_column] = label_value

    for column in cost_columns:
        summary_row[column] = round(pd.to_numeric(df[column], errors="coerce").fillna(0).sum(), 2)

    for column in sum_columns:
        summary_row[column] = int(pd.to_numeric(df[column], errors="coerce").fillna(0).sum())

    return pd.concat([df, pd.DataFrame([summary_row])], ignore_index=True)

st.set_page_config(page_title="AWS Stack Monitor", layout="wide")
st.title("AWS Stack Cost Monitor")
st.caption("Stack-first monthly cost tracking for CloudFormation-managed AWS infrastructure.")

with st.sidebar:
    st.header("AWS Config")
    available_profiles = [""] + get_available_profiles()
    profile = st.selectbox(
        "AWS profile",
        options=available_profiles,
        index=0,
        format_func=lambda option: option if option else "(default credential chain)",
    )
    region = st.text_input("Region", value="us-east-1")
    show_zero_cost_stacks = st.checkbox("Show $0 stacks", value=True)
    refresh_requested = st.button("Refresh data", width="stretch")

if refresh_requested:
    st.cache_data.clear()

month_windows = get_month_windows(date.today(), MONTH_COUNT)
display_month_windows = list(reversed(month_windows))
month_labels = [window["label"] for window in display_month_windows]
latest_window = month_windows[-1]
range_start = month_windows[0]["start"]
range_end = month_windows[-1]["end"]

try:
    with st.spinner("Loading CloudFormation inventory and Cost Explorer data..."):
        stack_inventory = load_stack_inventory(profile, region)
        hosted_zones = get_hosted_zone_record_counts(profile)
        total_costs_by_month = get_total_costs_by_month(profile, region, range_start, range_end)
        exact_stack_costs_by_month = get_costs_by_stack_by_month(profile, region, range_start, range_end)
        exact_service_costs_by_month = get_costs_by_stack_and_service_by_month(
            profile, region, range_start, range_end
        )
except Exception as exc:
    st.error(f"AWS connection failed: {exc}")
    st.stop()

unattributed_monthly = exact_stack_costs_by_month.get(UNTAGGED_LABEL, {})
unattributed_service_costs_by_month = exact_service_costs_by_month.get(UNTAGGED_LABEL, {})
(
    heuristic_stack_costs_by_month,
    heuristic_stack_service_costs_by_month,
    unattributed_service_rows,
    heuristic_stack_service_metadata,
    unresolved_monthly,
) = build_heuristic_allocations(
    stack_inventory,
    unattributed_service_costs_by_month,
    display_month_windows,
    hosted_zones,
)

stack_rows = build_stack_rows(
    stack_inventory,
    exact_stack_costs_by_month,
    heuristic_stack_costs_by_month,
    display_month_windows,
)
if not show_zero_cost_stacks:
    stack_rows = [row for row in stack_rows if row["Total ($)"] > 0]

stack_df = pd.DataFrame(stack_rows)
if not stack_df.empty:
    stack_df = stack_df.sort_values(
        by=[latest_window["label"], "Total ($)", "Stack"],
        ascending=[False, False, True],
    )

latest_total = total_costs_by_month.get(latest_window["key"], 0.0)
latest_exact_total = round(
    sum(
        monthly_costs.get(latest_window["key"], 0.0)
        for stack_name, monthly_costs in exact_stack_costs_by_month.items()
        if stack_name != UNTAGGED_LABEL
    ),
    2,
)
latest_heuristic_total = round(
    sum(
        monthly_costs.get(latest_window["key"], 0.0)
        for monthly_costs in heuristic_stack_costs_by_month.values()
    ),
    2,
)
latest_unattributed_total = unattributed_monthly.get(latest_window["key"], 0.0)
latest_unresolved_total = round(
    max(latest_unattributed_total - latest_heuristic_total, unresolved_monthly.get(latest_window["key"], 0.0)),
    2,
)

st.subheader("Monthly Overview")
top_metrics = st.columns(4)
top_metrics[0].metric(f"Account Spend | {latest_window['label']}", format_cost_display(latest_total))
top_metrics[1].metric("Exact Stack-Tagged", format_cost_display(latest_exact_total))
top_metrics[2].metric("Heuristic Stack Estimate", format_cost_display(latest_heuristic_total))
top_metrics[3].metric("Still Unattributed", format_cost_display(latest_unresolved_total))
st.caption(f"Showing {month_labels[0]} through {month_labels[-1]}.")
st.caption(
    "The stack table below is a blended view: exact Cost Explorer stack-tag attribution plus weighted "
    "heuristic shares of unattributed services such as ELB, Route 53, and VPC when CloudFormation "
    "resources suggest a likely owner."
)

untagged_breakdown = [
    f"{window['label']}: {format_currency_plain(unattributed_monthly.get(window['key'], 0.0))}"
    for window in month_windows
    if unattributed_monthly.get(window["key"], 0.0) > 0
]
if untagged_breakdown:
    st.warning(
        "Some spend is still unassigned to a CloudFormation stack. "
        + " | ".join(untagged_breakdown)
        + ". The app now keeps that unattributed pool separate and applies heuristic stack hints only when the CloudFormation inventory gives a plausible owner."
    )

unattributed_service_df = pd.DataFrame(unattributed_service_rows)
if not unattributed_service_df.empty:
    unattributed_service_df = unattributed_service_df.sort_values(
        by=[latest_window["label"], "Total ($)", "Billing Service"],
        ascending=[False, False, True],
    )
    unattributed_service_df = add_total_row(
        unattributed_service_df,
        "Billing Service",
        "TOTAL",
        month_labels + ["Total ($)"],
        [],
    )

    st.markdown("**Unattributed Services With Likely Stack Hints**")
    st.dataframe(
        apply_cost_display(unattributed_service_df, month_labels + ["Total ($)"]),
        width="stretch",
        hide_index=True,
    )

st.divider()

st.subheader("Stacks")
st.caption(
    "Blended stack totals combine exact stack-tagged spend with heuristic estimates from unattributed ELB, Route 53, and VPC services."
)
stack_filter = st.text_input("Filter stack names", value="")

if not stack_df.empty and stack_filter.strip():
    stack_df = stack_df[stack_df["Stack"].str.contains(stack_filter, case=False, na=False)]

if stack_df.empty:
    st.info(
        "No stack cost data was found. Make sure the "
        f"`{STACK_TAG_KEY}` cost allocation tag is active in AWS Billing."
    )
    st.stop()

stack_display_df = add_total_row(
    stack_df,
    "Stack",
    "TOTAL",
    month_labels + ["Exact Tagged ($)", "Heuristic ($)", "Total ($)"],
    ["Services", "Resources"],
)

st.dataframe(
    apply_cost_display(
        stack_display_df,
        month_labels + ["Exact Tagged ($)", "Heuristic ($)", "Total ($)"],
    ),
    width="stretch",
    hide_index=True,
    column_config={
        "Services": st.column_config.NumberColumn(format="%d"),
        "Resources": st.column_config.NumberColumn(format="%d"),
    },
)

st.divider()
st.subheader("Stack Details")

for row in stack_df.to_dict("records"):
    stack_name = row["Stack"]
    inventory = stack_inventory.get(
        stack_name,
        {
            "resource_count": 0,
            "resource_rows": [],
            "service_rows": [],
            "service_count": 0,
        },
    )
    exact_monthly_costs = exact_stack_costs_by_month.get(stack_name, {})
    heuristic_monthly_costs = heuristic_stack_costs_by_month.get(stack_name, {})
    latest_exact_stack_cost = exact_monthly_costs.get(latest_window["key"], 0.0)
    latest_heuristic_stack_cost = heuristic_monthly_costs.get(latest_window["key"], 0.0)
    latest_blended_stack_cost = round(latest_exact_stack_cost + latest_heuristic_stack_cost, 2)

    title = (
        f"{stack_name} | "
        f"{format_currency_plain(latest_blended_stack_cost)} in {latest_window['short_label']} | "
        f"{format_currency_plain(row['Total ($)'])} across {MONTH_COUNT} months"
    )
    with st.expander(title, expanded=False):
        detail_metrics = st.columns(4)
        detail_metrics[0].metric("Exact tagged | latest", format_cost_display(latest_exact_stack_cost))
        detail_metrics[1].metric("Heuristic | latest", format_cost_display(latest_heuristic_stack_cost))
        detail_metrics[2].metric("Blended 6-month total", format_cost_display(row["Total ($)"]))
        detail_metrics[3].metric(
            "Resources / services",
            f"{inventory['resource_count']} / {inventory['service_count']}",
        )

        blended_service_df = pd.DataFrame(
            build_combined_service_rows(
                inventory["service_rows"],
                exact_service_costs_by_month.get(stack_name, {}),
                heuristic_stack_service_costs_by_month.get(stack_name, {}),
                heuristic_stack_service_metadata.get(stack_name, {}),
                display_month_windows,
            )
        )
        if not blended_service_df.empty:
            blended_service_df = blended_service_df.sort_values(
                by=[latest_window["label"], "Blended Total ($)", "Service"],
                ascending=[False, False, True],
            )
            blended_service_df = add_total_row(
                blended_service_df,
                "Service",
                "TOTAL",
                month_labels + ["Exact Tagged ($)", "Heuristic ($)", "Blended Total ($)"],
                ["Resources"],
            )

            st.markdown("**Blended Service Summary**")
            st.caption(
                "Each row combines exact stack-tagged spend with heuristic estimated shares from unattributed services."
            )
            st.dataframe(
                apply_cost_display(
                    blended_service_df,
                    month_labels + ["Exact Tagged ($)", "Heuristic ($)", "Blended Total ($)"],
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Resources": st.column_config.NumberColumn(format="%d"),
                },
            )
        else:
            st.info("No service-level cost or inventory data is available for this stack.")

        resources_df = pd.DataFrame(inventory["resource_rows"])
        if not resources_df.empty:
            st.markdown("**Resources**")
            st.dataframe(
                resources_df.sort_values(by=["Service", "Logical ID"]),
                width="stretch",
                hide_index=True,
            )
        elif stack_name == UNTAGGED_LABEL:
            st.info(
                "Untagged spend is not tied to a CloudFormation stack, so there is no resource inventory to show."
            )
        else:
            st.info("CloudFormation has no active resources for this stack.")
