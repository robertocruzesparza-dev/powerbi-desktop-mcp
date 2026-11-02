# DAX patterns

Contents: ratios, time intelligence, ranking, running totals, segmentation, disconnected selectors. Replace table and column names; test every pattern with `validate_dax_live`.

## Percent of total

```dax
Pct of Total =
VAR Part = [Total Sales]
VAR Whole = CALCULATE([Total Sales], REMOVEFILTERS(Product))
RETURN DIVIDE(Part, Whole)
```

`REMOVEFILTERS(Product)` clears only that table; use `ALLSELECTED(Product)` for percent of what the user has selected.

## Time intelligence (needs a marked date table)

```dax
Sales YTD = TOTALYTD([Total Sales], 'Date'[Date])
Sales PY  = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))
Sales YoY % = VAR PY = [Sales PY] RETURN DIVIDE([Total Sales] - PY, PY)
Sales 3M Avg = AVERAGEX(DATESINPERIOD('Date'[Date], MAX('Date'[Date]), -3, MONTH), [Total Sales])
```

For a school-year (fiscal) end, pass the year-end date as the third argument of `TOTALYTD`, for example `"6/30"`.

## Ranking and top N

```dax
Region Rank = RANKX(ALLSELECTED(Region[Region]), [Total Sales], , DESC, DENSE)
Top 5 Sales = CALCULATE([Total Sales], KEEPFILTERS(TOPN(5, ALLSELECTED(Product[Name]), [Total Sales])))
```

## Running total

```dax
Running Total =
CALCULATE([Total Sales], FILTER(ALLSELECTED('Date'[Date]), 'Date'[Date] <= MAX('Date'[Date])))
```

## Distinct counts and conditional counts

```dax
Students Served = DISTINCTCOUNT(Enrollment[StudentKey])
Large Orders = CALCULATE(COUNTROWS(Sales), Sales[Amount] > 100)
```

## Disconnected selector (what-if or metric switch)

```dax
Selected Metric =
SWITCH(SELECTEDVALUE(MetricSelector[Metric], "Sales"),
    "Sales", [Total Sales], "Orders", [Order Count], BLANK())
```

## Safe blanks

```dax
Margin % = DIVIDE([Profit], [Revenue])          -- blank when Revenue is 0 or blank
Has Data = NOT ISBLANK([Total Sales])
```

## Smells the linter reports

| Rule | Smell | Preferred |
|---|---|---|
| DAX001 | IFERROR | DIVIDE or explicit test |
| DAX002/003 | FILTER(ALL(...)) or whole-table FILTER | boolean CALCULATE filter |
| DAX005 | nested CALCULATE | one CALCULATE with all filters |
| DAX007 | COUNTROWS(FILTER(...)) | CALCULATE(COUNTROWS(...), predicate) |
| DAX009 | FORMAT() in a measure | formatString |
| DAX011 | `/` | DIVIDE |
