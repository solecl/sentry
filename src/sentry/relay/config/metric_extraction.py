import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Literal, Optional, Sequence, Tuple, TypedDict, Union, cast

from snuba_sdk import BooleanCondition, Column, Condition
from typing_extensions import NotRequired

from sentry import features
from sentry.api.endpoints.project_transaction_threshold import DEFAULT_THRESHOLD
from sentry.constants import DataCategory
from sentry.incidents.models import AlertRule, AlertRuleStatus
from sentry.models import (
    Project,
    ProjectTransactionThreshold,
    ProjectTransactionThresholdOverride,
    TransactionMetric,
)
from sentry.search.events.builder import QueryBuilder
from sentry.snuba.dataset import Dataset

logger = logging.getLogger(__name__)

# GENERIC METRIC EXTRACTION


# Name component of MRIs used for custom alert metrics.
# TODO: Move to a common module.
CUSTOM_ALERT_METRIC_NAME = "transactions/on-demand"

# Version of the metric extraction config.
_METRIC_EXTRACTION_VERSION = 1

# Maximum number of custom metrics that can be extracted for alert rules with
# advanced filter expressions.
_MAX_ALERT_METRICS = 100

# Base type for conditions to evaluate on payloads.
# TODO: Streamline with dynamic sampling.
RuleCondition = Union["LogicalRuleCondition", "ComparingRuleCondition", "NotRuleCondition"]


class ComparingRuleCondition(TypedDict):
    """RuleCondition that compares a named field to a reference value."""

    op: Literal["eq", "gt", "gte", "lt", "lte", "glob"]
    name: str
    value: Any


class LogicalRuleCondition(TypedDict):
    """RuleCondition that applies a logical operator to a sequence of conditions."""

    op: Literal["and", "or"]
    inner: Sequence[RuleCondition]


class NotRuleCondition(TypedDict):
    """RuleCondition that negates an inner condition."""

    op: Literal["not"]
    inner: RuleCondition


class TagSpec(TypedDict):
    """
    Configuration for a tag to add to a metric.

    Tags values can be static if defined through `value` or dynamically queried
    from the payload if defined through `field`. These two options are mutually
    exclusive, behavior is undefined if both are specified.
    """

    key: str
    field: NotRequired[str]
    value: NotRequired[str]
    condition: NotRequired[RuleCondition]


class MetricSpec(TypedDict):
    """
    Specification for a metric to extract from some data.

    The metric type is given as part of the MRI (metric reference identifier)
    which must follow the form: `<type>:<namespace>/<name>@<unit>`.

    How the metric's value is obtained depends on the metric type:
     - Counter metrics are a special case, since the default product counters do
       not count any specific field but rather the occurrence of the event. As
       such, there is no value expression, and the field is set to `None`.
       Semantics of specifying remain undefined at this point.
     - Distribution metrics require a numeric value.
     - Set metrics require a string value, which is then emitted into the set as
       unique value. Insertion of numbers and other types is undefined.
    """

    category: Literal["transaction"]
    mri: str
    field: NotRequired[Optional[str]]
    condition: NotRequired[RuleCondition]
    tags: NotRequired[Sequence[TagSpec]]


class MetricExtractionConfig(TypedDict):
    """Configuration for generic extraction of metrics from all data categories."""

    version: int
    metrics: List[MetricSpec]


_SNUBA_TO_RELAY_FIELDS = {
    # "browser.name": "event.browser.name",  # TODO is a function
    "contexts[geo.country_code]": "event.geo.country_code",
    "http_method": "event.http.method",
    # "http.status_code": "event.http.status_code",  TODO is a function
    # "os.name": "event.os.name",  TODO is a function
    "release": "event.release",
    "transaction_op": "event.transaction.op",
    "transaction_status": "event.transaction.status",
    "duration": "event.duration",
    "measurements[cls]": "event.measurements.cls",
    "measurements[fcp]": "event.measurements.fcp",
    "measurements[fid]": "event.measurements.fid",
    "measurements[fp]": "event.measurements.fp",
    "measurements[lcp]": "event.measurements.lcp",
    "measurements[ttfb]": "event.measurements.ttfb",
    "measurements[ttfb.requesttime]": "event.measurements.ttfb.requesttime",
}

# TODO: support count_if
_AGGREGATE_TO_METRIC_TYPE = {
    "count": "c",
    "avg": "d",
    "quantile(0.50)": "d",
    "quantile(0.75)": "d",
    "quantile(0.95)": "d",
    "quantile(0.99)": "d",
    "max": "d",
    # not supported yet
    "percentile": None,
    "failure_rate": None,
    "apdex": None,
}


def get_metric_extraction_config(project: Project) -> Optional[MetricExtractionConfig]:
    """
    Returns generic metric extraction config for the given project.

    This requires respective feature flags to be enabled. At the moment, metrics
    for the following models are extracted:
     - Performance alert rules which advanced filter expressions.
    """

    if not features.has("organizations:on-demand-metrics-extraction", project.organization):
        return None

    alerts = (
        AlertRule.objects.fetch_for_project(project)
        .filter(status=AlertRuleStatus.PENDING.value)
        .select_related("snuba_query")
    )

    metrics: List[MetricSpec] = []
    for alert in alerts:
        if metric := _convert_alert_to_metric(alert):
            metrics.append(metric)

    if not metrics:
        return None

    if len(metrics) > _MAX_ALERT_METRICS:
        logger.error("Too many custom alert metrics for project")
        metrics = metrics[:_MAX_ALERT_METRICS]

    return {
        "version": _METRIC_EXTRACTION_VERSION,
        "metrics": metrics,
    }


def _convert_alert_to_metric(alert: AlertRule) -> Optional[MetricSpec]:
    if not alert.is_custom_metric:
        return None

    try:
        query = alert.snuba_query.query
        field, metric_type = _extract_field_info(alert.snuba_query.aggregate)

        condition = _convert_alert_query_to_condition(query)
        query_hash = _get_query_hash(field, query)

        return {
            "category": DataCategory.TRANSACTION.api_name(),
            "mri": f"{metric_type}:{CUSTOM_ALERT_METRIC_NAME}@none",
            "field": field,
            "condition": condition,
            "tags": [{"key": "query_hash", "value": query_hash}],
        }
    except Exception as e:
        logger.error(e, exc_info=True)
        return None


def _extract_field_info(aggregate: str) -> Tuple[Optional[str], str]:
    select = _get_query_builder().resolve_column(aggregate, False)

    metric_type = _AGGREGATE_TO_METRIC_TYPE.get(select.function)
    assert metric_type, f"Unsupported aggregate function {select.function}"

    if metric_type == "c":
        assert not select.parameters, "Count should not have parameters"
        return None, metric_type
    else:
        assert len(select.parameters) == 1, "Only one parameter is supported"

        name = select.parameters[0].name
        assert name in _SNUBA_TO_RELAY_FIELDS, f"Unsupported field {name}"

        return _SNUBA_TO_RELAY_FIELDS[name], metric_type


def _convert_alert_query_to_condition(query: str) -> RuleCondition:
    where, having = _get_query_builder().resolve_conditions(query, False)

    assert where, "Query should not use on demand metrics"
    assert not having, "Having conditions are not supported"

    where = [c for c in (_convert_condition(c) for c in where) if c is not None]

    if len(where) == 1:
        return where[0]
    else:
        return {"op": "and", "inner": where}


def _convert_condition(condition: Union[Condition, BooleanCondition]) -> Optional[RuleCondition]:
    if isinstance(condition, BooleanCondition):
        return cast(
            RuleCondition,
            {
                "op": condition.op.name.lower(),
                "inner": [_convert_condition(c) for c in condition.conditions],
            },
        )

    assert isinstance(condition, Condition), f"Unsupported condition type {type(condition)}"

    # TODO: Currently we do not support function conditions like count_if
    if not isinstance(condition.lhs, Column):
        return None

    assert condition.lhs.name in _SNUBA_TO_RELAY_FIELDS, f"Unsupported field {condition.lhs.name}"

    return {
        "op": condition.op.name.lower(),
        "name": _SNUBA_TO_RELAY_FIELDS[condition.lhs.name],
        "value": condition.rhs,
    }


def _get_query_hash(name: Optional[str], filters: str) -> str:
    return hashlib.shake_128(bytes(f"{name};{filters}", encoding="ascii")).hexdigest(4)


def _get_query_builder() -> QueryBuilder:
    return QueryBuilder(
        dataset=Dataset.Transactions,
        params={"start": datetime.now(), "end": datetime.now()},
    )


# CONDITIONAL TAGGING


class MetricConditionalTaggingRule(TypedDict):
    condition: RuleCondition
    targetMetrics: Sequence[str]
    targetTag: str
    tagValue: str


_TRANSACTION_METRICS_TO_RULE_FIELD = {
    TransactionMetric.LCP.value: "event.measurements.lcp.value",
    TransactionMetric.DURATION.value: "event.duration",
}

_SATISFACTION_TARGET_METRICS = (
    "s:transactions/user@none",
    "d:transactions/duration@millisecond",
    "d:transactions/measurements.lcp@millisecond",
)

_SATISFACTION_TARGET_TAG = "satisfaction"

_HISTOGRAM_OUTLIERS_TARGET_METRICS = {
    "duration": "d:transactions/duration@millisecond",
    "lcp": "d:transactions/measurements.lcp@millisecond",
    "fcp": "d:transactions/measurements.fcp@millisecond",
}


@dataclass
class _DefaultThreshold:
    metric: int
    threshold: int


_DEFAULT_THRESHOLD = _DefaultThreshold(
    metric=TransactionMetric[DEFAULT_THRESHOLD["metric"].upper()].value,
    threshold=int(DEFAULT_THRESHOLD["threshold"]),
)


def get_metric_conditional_tagging_rules(
    project: Project,
) -> Sequence[MetricConditionalTaggingRule]:
    rules: List[MetricConditionalTaggingRule] = []

    # transaction-specific overrides must precede the project-wide threshold in the list of rules.
    for threshold_override in project.projecttransactionthresholdoverride_set.all().order_by(
        "transaction"
    ):
        rules.extend(
            _threshold_to_rules(
                threshold_override,
                [
                    {
                        "op": "eq",
                        "name": "event.transaction",
                        "value": threshold_override.transaction,
                    }
                ],
            )
        )

    # Rules are processed top-down. The following is a fallback for when
    # there's no transaction-name-specific rule:

    try:
        threshold = ProjectTransactionThreshold.objects.get(project=project)
        rules.extend(_threshold_to_rules(threshold, []))
    except ProjectTransactionThreshold.DoesNotExist:
        rules.extend(_threshold_to_rules(_DEFAULT_THRESHOLD, []))

    rules.extend(_HISTOGRAM_OUTLIER_RULES)

    return rules


def _threshold_to_rules(
    threshold: Union[
        ProjectTransactionThreshold, ProjectTransactionThresholdOverride, _DefaultThreshold
    ],
    extra_conditions: Sequence[RuleCondition],
) -> Sequence[MetricConditionalTaggingRule]:
    frustrated: MetricConditionalTaggingRule = {
        "condition": {
            "op": "and",
            "inner": [
                {
                    "op": "gt",
                    "name": _TRANSACTION_METRICS_TO_RULE_FIELD[cast(int, threshold.metric)],
                    # The frustration threshold is always four times the threshold
                    # (see https://docs.sentry.io/product/performance/metrics/#apdex)
                    "value": cast(int, threshold.threshold) * 4,
                },
                *extra_conditions,
            ],
        },
        "targetMetrics": _SATISFACTION_TARGET_METRICS,
        "targetTag": _SATISFACTION_TARGET_TAG,
        "tagValue": "frustrated",
    }
    tolerated: MetricConditionalTaggingRule = {
        "condition": {
            "op": "and",
            "inner": [
                {
                    "op": "gt",
                    "name": _TRANSACTION_METRICS_TO_RULE_FIELD[cast(int, threshold.metric)],
                    "value": threshold.threshold,
                },
                *extra_conditions,
            ],
        },
        "targetMetrics": _SATISFACTION_TARGET_METRICS,
        "targetTag": _SATISFACTION_TARGET_TAG,
        "tagValue": "tolerated",
    }
    satisfied: MetricConditionalTaggingRule = {
        "condition": {"op": "and", "inner": list(extra_conditions)},
        "targetMetrics": _SATISFACTION_TARGET_METRICS,
        "targetTag": _SATISFACTION_TARGET_TAG,
        "tagValue": "satisfied",
    }

    # Order is important here, as rules for a particular tag name are processed
    # top-down, and rules are skipped if the tag has already been defined by a
    # previous rule.
    #
    # if duration > 4000 {
    #     frustrated
    # } else if duration > 1000 {
    #     tolerated
    # } else {
    #     satisfied
    # }
    return [frustrated, tolerated, satisfied]


# These JSON results are generated by S&S using internal data-tooling. The
# roughly equivalent ClickHouse query that we used to use instead is:
#
# SELECT
#     platform,
#     transaction_op AS op,
#     uniqCombined64(project_id) AS c,
#     quantiles(0.25, 0.75)(duration) as duration,
#     quantiles(0.25, 0.75)(measurements.value[indexOf(measurements.key, 'lcp')]) as lcp,
#     quantiles(0.25, 0.75)(measurements.value[indexOf(measurements.key, 'fcp')]) as fcp
# FROM transactions_dist
# WHERE timestamp > subtractHours(now(), 48)
# GROUP BY
#     platform,
#     op
# ORDER BY c DESC
# LIMIT 50
# FORMAT CSVWithNames
_HISTOGRAM_OUTLIERS_QUERY_RESULTS = [
    {
        "platform": "javascript",
        "op": "pageload",
        "c": "55927",
        "duration": ["0", "1539", "2813", "5185", "1678818846004"],
        "lcp": [
            "-58.39991569519043",
            "730.3001880645752",
            "1364.1000000238421",
            "2533.2000255584717",
            "7160348981.6997051",
        ],
        "fcp": [
            "-30.0",
            "578.29999923706055",
            "1051.0001182556152",
            "1908.10000000149",
            "4295032969.0001011",
        ],
    },
    {
        "platform": "javascript",
        "op": "navigation",
        "c": "46130",
        "duration": ["0", "372", "964", "1287", "1678819998036"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "http.server",
        "c": "20286",
        "duration": ["0", "3", "23", "98", "7169297964"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "http.server",
        "c": "16548",
        "duration": ["0", "2", "20", "128", "40043925665"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "php",
        "op": "http.server",
        "c": "11844",
        "duration": ["0", "35", "90", "249", "194551915"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "javascript",
        "op": "ui.load",
        "c": "5586",
        "duration": ["0", "1419", "3849", "50909", "1678715114066"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "celery.task",
        "c": "2936",
        "duration": ["0", "32", "94", "403", "462304451"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "rails.request",
        "c": "2719",
        "duration": ["0", "7", "27", "107", "411239453"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "queue.task.celery",
        "c": "2122",
        "duration": ["0", "29", "122", "681", "281861579"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "function.nextjs",
        "c": "2048",
        "duration": ["0", "1", "26", "127", "1047980"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "cocoa",
        "op": "ui.load",
        "c": "2025",
        "duration": ["0", "135", "554", "698", "1678189573840"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "csharp",
        "op": "http.server",
        "c": "1951",
        "duration": ["0", "1", "15", "82", "683064520"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "http.server",
        "c": "1944",
        "duration": ["0", "7", "20", "92", "230606309"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "java",
        "op": "ui.load",
        "c": "1867",
        "duration": ["0", "145", "291", "831", "1678830256706"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "java",
        "op": "http.server",
        "c": "1772",
        "duration": ["0", "2", "9", "63", "335196060"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "awslambda.handler",
        "c": "1522",
        "duration": ["0", "19", "103", "451", "2274015"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "serverless.function",
        "c": "1046",
        "duration": ["0", "29", "52", "120", "32730840"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "function.aws.lambda",
        "c": "915",
        "duration": ["0", "61", "206", "454", "8143646"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "javascript",
        "op": "default",
        "c": "850",
        "duration": ["0", "0", "237", "804", "1678679274843"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "function.aws",
        "c": "821",
        "duration": ["0", "0", "75", "366", "899160"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "active_job",
        "c": "729",
        "duration": ["0", "31", "153", "288", "14992111"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "other",
        "op": "navigation",
        "c": "689",
        "duration": ["0", "1102", "2629", "3003", "448059236223"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "queue.active_job",
        "c": "629",
        "duration": ["0", "25", "112", "1216", "202727763"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "sidekiq",
        "c": "569",
        "duration": ["0", "14", "69", "246", "34998169"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "other",
        "op": "pageload",
        "c": "551",
        "duration": ["988", "3000", "3000", "3000", "3700"],
        "lcp": [
            "4589.8220456729478",
            "4589.8220456729478",
            "4589.8220456729478",
            "4589.8220456729478",
            "4589.8220456729478",
        ],
        "fcp": [
            "2057.7001571655273",
            "3384.3555060724457",
            "3384.3555060724457",
            "3384.3555060724457",
            "3384.3555060724457",
        ],
    },
    {
        "platform": "php",
        "op": "console.command",
        "c": "462",
        "duration": ["0", "61", "150", "417", "3607425204"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "other",
        "op": "middleware.nextjs",
        "c": "447",
        "duration": ["0", "0", "0", "0", "185123"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "queue.sidekiq",
        "c": "447",
        "duration": ["0", "18", "145", "579", "24701323"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "transaction",
        "c": "446",
        "duration": ["0", "5", "20", "87", "602756293"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "cocoa",
        "op": "ui.action",
        "c": "444",
        "duration": ["0", "244", "1057", "2783", "498994"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "default",
        "c": "418",
        "duration": ["0", "2", "69", "423", "24534033"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "cocoa",
        "op": "ui.action.click",
        "c": "400",
        "duration": ["0", "223", "1127", "3797", "84802486"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "asgi.server",
        "c": "346",
        "duration": ["0", "158", "298", "1291", "33673793505"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "go",
        "op": "http.server",
        "c": "302",
        "duration": ["0", "0", "0", "4", "167496305"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "php",
        "op": "sentry.test",
        "c": "280",
        "duration": ["0", "0", "0", "1", "223"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "websocket.server",
        "c": "255",
        "duration": ["0", "0", "1", "4", "1065382"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "java",
        "op": "ui.action.click",
        "c": "207",
        "duration": ["0", "343", "1271", "3560", "228385283"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "other",
        "op": "http.server",
        "c": "200",
        "duration": ["0", "0", "7", "57", "7954687"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "test",
        "c": "188",
        "duration": ["0", "12", "409", "1080", "263783678"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "node",
        "op": "gql",
        "c": "181",
        "duration": ["0", "16", "39", "135", "1503274"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "default",
        "c": "181",
        "duration": ["0", "5", "11", "67", "108818494"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "ruby",
        "op": "rails.action_cable",
        "c": "177",
        "duration": ["0", "0", "0", "5", "291392"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "php",
        "op": "queue.process",
        "c": "167",
        "duration": ["0", "26", "68", "232", "1641192"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "websocket.server",
        "c": "160",
        "duration": ["0", "1", "2", "6226", "518009460"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "rq.task",
        "c": "151",
        "duration": ["2", "175", "388", "490", "73547039"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "task",
        "c": "147",
        "duration": ["0", "9", "54", "336", "12559622"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "java",
        "op": "ui.action.swipe",
        "c": "139",
        "duration": ["0", "966", "2343", "5429", "56370777"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "python",
        "op": "queue.task.rq",
        "c": "136",
        "duration": ["2", "113", "277", "913", "14400609"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "java",
        "op": "navigation",
        "c": "125",
        "duration": ["0", "327", "1091", "2657", "123162256"],
        "lcp": [],
        "fcp": [],
    },
    {
        "platform": "java",
        "op": "ui.action.scroll",
        "c": "107",
        "duration": ["1", "400", "951", "2158", "45034933"],
        "lcp": [],
        "fcp": [],
    },
]


def _parse_percentiles(
    value: Union[Tuple[()], Tuple[str, str, str, str, str]]
) -> Tuple[float, float]:
    if not value:
        return 0, 0
    _min, p25, _p50, p75, _max = map(float, value)
    return p25, p75


def _produce_histogram_outliers(query_results: Any) -> Sequence[MetricConditionalTaggingRule]:
    rules: List[MetricConditionalTaggingRule] = []
    for row in query_results:
        platform = row["platform"]
        op = row["op"]
        duration = row["duration"]
        lcp = row["lcp"]
        fcp = row["fcp"]
        duration_p25, duration_p75 = _parse_percentiles(duration)
        lcp_p25, lcp_p75 = _parse_percentiles(lcp)
        fcp_p25, fcp_p75 = _parse_percentiles(fcp)

        for metric, p25, p75 in (
            ("duration", duration_p25, duration_p75),
            ("lcp", lcp_p25, lcp_p75),
            ("fcp", fcp_p25, fcp_p75),
        ):
            if p25 == p75 == 0:
                # default values from clickhouse if no data is present
                continue

            rules.append(
                {
                    "condition": {
                        "op": "and",
                        "inner": [
                            {"op": "eq", "name": "event.contexts.trace.op", "value": op},
                            {"op": "eq", "name": "event.platform", "value": platform},
                            # This is in line with https://github.com/getsentry/sentry/blob/63308b3f2256fe2f24da43a951154d0ef2218d19/src/sentry/snuba/discover.py#L1728-L1729=
                            # See also https://en.wikipedia.org/wiki/Outlier#Tukey's_fences
                            {
                                "op": "gte",
                                "name": "event.duration",
                                "value": p75 + 3 * abs(p75 - p25),
                            },
                        ],
                    },
                    "targetMetrics": [_HISTOGRAM_OUTLIERS_TARGET_METRICS[metric]],
                    "targetTag": "histogram_outlier",
                    "tagValue": "outlier",
                }
            )

    rules.append(
        {
            "condition": {
                "op": "and",
                "inner": [
                    {"op": "gte", "name": "event.duration", "value": 0},
                ],
            },
            "targetMetrics": list(_HISTOGRAM_OUTLIERS_TARGET_METRICS.values()),
            "targetTag": "histogram_outlier",
            "tagValue": "inlier",
        }
    )

    rules.append(
        {
            "condition": {"op": "and", "inner": []},
            "targetMetrics": list(_HISTOGRAM_OUTLIERS_TARGET_METRICS.values()),
            "targetTag": "histogram_outlier",
            "tagValue": "outlier",
        }
    )

    return rules


_HISTOGRAM_OUTLIER_RULES = _produce_histogram_outliers(_HISTOGRAM_OUTLIERS_QUERY_RESULTS)
