"""Upload asdr data to t3 table so EpiViz can access it for plotting."""

import pandas as pd

from cascade.core.db import cursor, db_queries
from cascade.input_data.db import AGE_GROUP_SET_ID
from cascade.input_data.db.locations import get_descendants, location_hierarchy


from cascade.core.log import getLoggers

CODELOG, MATHLOG = getLoggers(__name__)


def _asdr_in_t3(execution_context, model_version_id):
    """Checks if data for the current model_version_id already exists in tier 3.
    """
    query = """
    SELECT DISTINCT location_id
    FROM epi.t3_model_version_asdr
    WHERE model_version_id = %(model_version_id)s
    """
    with cursor(execution_context) as c:
        c.execute(query, args={"model_version_id": model_version_id})
        location_rows = c.fetchall()

    return [row[0] for row in location_rows]


def get_asdr_data(gbd_round_id, location_and_children):
    r"""Gets the age-specific death rate from IHME databases.
    This is :math:`{}_nm_x`, the mortality rate.
    """
    demo_dict = db_queries.get_demographics(gbd_team="epi", gbd_round_id=gbd_round_id)
    age_group_ids = demo_dict["age_group_id"]
    sex_ids = demo_dict["sex_id"]

    asdr = db_queries.get_envelope(
        location_id=location_and_children,
        year_id=-1,
        gbd_round_id=gbd_round_id,
        age_group_id=age_group_ids,
        sex_id=sex_ids,
        with_hiv=True,
        rates=True,
    ).drop(columns=["run_id"])

    asdr = asdr[asdr["mean"].notnull()]

    return asdr


def _upload_asdr_data_to_tier_3(gbd_round_id, cursor, model_version_id, asdr_data):
    """Uploads asdr data to tier 3 attached to the current model_version_id.
    """

    insert_query = f"""
        INSERT INTO epi.t3_model_version_asdr (
            model_version_id,
            year_id,
            location_id,
            sex_id,
            age_group_id,
            mean,
            upper,
            lower,
            age_upper,
            age_lower
        ) VALUES (
            {model_version_id}, {", ".join(["%s"]*9)}
        )
    """

    age_group_data = db_queries.get_age_metadata(
        age_group_set_id=AGE_GROUP_SET_ID, gbd_round_id=gbd_round_id
    )[["age_group_id", "age_group_years_start", "age_group_years_end"]]

    age_group_data = age_group_data.rename(columns={
        "age_group_years_start": "age_lower",
        "age_group_years_end": "age_upper"
    })
    asdr_data = asdr_data.merge(age_group_data, how="left", on="age_group_id")
    asdr_data = asdr_data.where(pd.notnull(asdr_data), None)

    ordered_cols = [
        "year_id",
        "location_id",
        "sex_id",
        "age_group_id",
        "mean",
        "upper",
        "lower",
        "age_upper",
        "age_lower",
    ]
    asdr_data = asdr_data[ordered_cols]
    cursor.executemany(insert_query, asdr_data.values.tolist())
    CODELOG.debug(f"uploaded {len(asdr_data)} lines of asdr data")


def load_asdr_to_t3(execution_context, model_version_id, parent_location_id, gbd_round_id) -> bool:
    """
    Upload to t3_model_version_asdr if it's not already there.
    """
    database = execution_context.parameters.database
    locations = location_hierarchy(execution_context)
    location_and_children = get_descendants(locations, parent_location_id, children_only=True, include_parent=True)
    locations_with_asdr_in_t3 = _asdr_in_t3(execution_context, model_version_id)
    missing_from_t3 = set(location_and_children) - set(locations_with_asdr_in_t3)
    if missing_from_t3:
        CODELOG.info(
            f"""Uploading asdr data for model_version_id
            {model_version_id} on '{database}'"""
        )
        asdr_data = get_asdr_data(gbd_round_id, list(missing_from_t3))

        with cursor(execution_context) as c:
            _upload_asdr_data_to_tier_3(gbd_round_id, c, model_version_id, asdr_data)

        return True
    else:
        CODELOG.info(
            f"""asdr data for model_version_id {model_version_id}
            on '{database}' already exists, doing nothing."""
        )
        return False
