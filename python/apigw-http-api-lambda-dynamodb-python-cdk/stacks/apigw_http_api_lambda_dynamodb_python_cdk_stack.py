# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
from aws_cdk import (
    Stack,
    aws_dynamodb as dynamodb_,
    aws_lambda as lambda_,
    aws_apigateway as apigw_,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_cloudwatch as cloudwatch_,
    aws_logs as logs_,
    aws_cloudtrail as cloudtrail_,
    aws_s3 as s3_,
    Duration,
    RemovalPolicy,
)
from constructs import Construct

TABLE_NAME = "demo_table"


class ApigwHttpApiLambdaDynamodbPythonCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC Flow Logs Log Group
        vpc_flow_log_group = logs_.LogGroup(
            self,
            "VpcFlowLogs",
            retention=logs_.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # VPC
        vpc = ec2.Vpc(
            self,
            "Ingress",
            cidr="10.1.0.0/16",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private-Subnet", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24
                )
            ],
        )

        # Enable VPC Flow Logs
        vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(vpc_flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )
        
        # Create VPC endpoint
        dynamo_db_endpoint = ec2.GatewayVpcEndpoint(
            self,
            "DynamoDBVpce",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            vpc=vpc,
        )

        # This allows to customize the endpoint policy
        dynamo_db_endpoint.add_to_policy(
            iam.PolicyStatement(  # Restrict to listing and describing tables
                principals=[iam.AnyPrincipal()],
                actions=[                "dynamodb:DescribeStream",
                "dynamodb:DescribeTable",
                "dynamodb:Get*",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:CreateTable",
                "dynamodb:Delete*",
                "dynamodb:Update*",
                "dynamodb:PutItem"],
                resources=["*"],
            )
        )

        # Create DynamoDb Table
        demo_table = dynamodb_.Table(
            self,
            TABLE_NAME,
            partition_key=dynamodb_.Attribute(
                name="id", type=dynamodb_.AttributeType.STRING
            ),
        )

        # Create the Lambda function to receive the request
        api_hanlder = lambda_.Function(
            self,
            "ApiHandler",
            function_name="apigw_handler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("lambda/apigw-handler"),
            handler="index.handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            memory_size=1024,
            timeout=Duration.minutes(5),
            tracing=lambda_.Tracing.ACTIVE,
            log_retention=logs_.RetentionDays.SIX_MONTHS,
        )

        # grant permission to lambda to write to demo table
        demo_table.grant_write_data(api_hanlder)
        api_hanlder.add_environment("TABLE_NAME", demo_table.table_name)

        # API Gateway Access Logs Log Group
        api_log_group = logs_.LogGroup(
            self,
            "ApiGatewayAccessLogs",
            retention=logs_.RetentionDays.SIX_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create API Gateway
        api = apigw_.LambdaRestApi(
            self,
            "Endpoint",
            handler=api_hanlder,
            deploy_options=apigw_.StageOptions(
                tracing_enabled=True,
                access_log_destination=apigw_.LogGroupLogDestination(api_log_group),
                access_log_format=apigw_.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
            ),
        )

        # CloudWatch Alarms
        lambda_error_alarm = cloudwatch_.Alarm(
            self,
            "LambdaErrorAlarm",
            metric=api_hanlder.metric_errors(),
            threshold=1,
            evaluation_periods=1,
            alarm_description="Alert when Lambda function errors occur",
        )

        api_error_metric = cloudwatch_.Metric(
            namespace="AWS/ApiGateway",
            metric_name="5XXError",
            dimensions_map={
                "ApiName": api.rest_api_name,
            },
            statistic="Sum",
            period=Duration.minutes(1),
        )

        api_error_alarm = cloudwatch_.Alarm(
            self,
            "ApiGateway5XXAlarm",
            metric=api_error_metric,
            threshold=1,
            evaluation_periods=1,
            alarm_description="Alert when API Gateway returns 5XX errors",
        )

        # S3 Bucket for CloudTrail Logs
        trail_bucket = s3_.Bucket(
            self,
            "CloudTrailBucket",
            encryption=s3_.BucketEncryption.S3_MANAGED,
            block_public_access=s3_.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3_.LifecycleRule(
                    expiration=Duration.days(90)
                )
            ],
        )

        # CloudTrail for DynamoDB Data Events
        trail = cloudtrail_.Trail(
            self,
            "CloudTrail",
            bucket=trail_bucket,
            is_multi_region_trail=False,
        )

        trail.log_all_lambda_data_events()
        trail.add_lambda_event_selector([api_hanlder])
