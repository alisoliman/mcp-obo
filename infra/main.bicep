extension microsoftGraphV1

@description('Short name used to keep resource names stable across azd runs.')
param environmentName string

@description('Azure region for AKS, ACR, and the public IP.')
param location string = resourceGroup().location

@description('Azure region for Microsoft Foundry.')
param foundryLocation string = 'swedencentral'

@description('Object ID of the interactive user running azd.')
param principalId string

@description('Single-node VM size for the demo cluster.')
param nodeVmSize string = 'Standard_B2s'

@description('Capacity in thousands of tokens per minute for gpt-5.6-terra.')
param modelCapacity int = 100

@description('''Deploy the Azure Databricks workspace and request the delegated
Databricks permission. Set to false for the Graph-only accelerator: the MCP
server then registers no Databricks tools, because DATABRICKS_HOST is never
produced.''')
param deployDatabricks bool = true

@description('''Optional app id allowed to request the MCP scope, for automated
verification only. Empty (the default) means the chat client is the only
pre-authorized caller. Set it to the Azure CLI app id
04b07795-8ddb-461a-bbee-02f9e1bf7b46 to let `test_databricks_e2e.py` mint a
delegated token with `az account get-access-token`. Nothing in the running
system needs this.''')
param harnessClientAppId string = ''

var suffix = uniqueString(subscription().id, resourceGroup().id, environmentName)
var aksName = take('aks-${environmentName}-${suffix}', 63)
var acrName = 'acr${suffix}'
var foundryName = take('foundry-${environmentName}-${suffix}', 64)
var foundryProjectName = take('${environmentName}-orders', 64)
var namespaceName = 'orders'
var serverServiceAccountName = 'fastmcp-orders'
var clientServiceAccountName = 'order-agent-chat'
var serverImageName = 'fastmcp-order-server'
var clientImageName = 'order-agent-chat'
var ingressPublicIpName = 'pip-${environmentName}-${suffix}'
var appDnsLabel = take('orders-${environmentName}-${suffix}', 63)
var scopeValue = 'Mcp.Tools.ReadWrite'
var serverAppUniqueName = 'fastmcp-order-server-${suffix}'
var clientAppUniqueName = 'fastmcp-order-client-${suffix}'
var scopeId = guid(serverAppUniqueName, scopeValue)
var graphAppId = '00000003-0000-0000-c000-000000000000'
var graphUserReadScopeId = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'
var modelName = 'gpt-5.6-terra'
var modelVersion = '2026-07-09'
// The Azure Databricks resource application. Unlike Graph, this app id is the
// same in every tenant, and `user_impersonation` is its only delegated scope —
// the one the OBO exchange asks for on the caller's behalf.
var databricksAppId = '2ff814a6-3304-4ab8-85cb-cd0e6f879c1d'
var databricksUserImpersonationScopeId = '739272be-e143-11e8-9f32-f2801f1b9fd1'
var databricksWorkspaceName = take('dbw-${environmentName}-${suffix}', 30)
var databricksManagedRgName = 'rg-${databricksWorkspaceName}-managed'
// Catalog names must be unique inside a metastore, and a metastore is shared by
// every workspace in a region. Suffixing keeps two deployments from colliding.
// The bootstrap falls back to the catalog Azure provisions with the workspace
// when the metastore has no storage root, and reports which one it used.
var databricksCatalogName = toLower('mcp_obo_${suffix}')
var databricksSchemaName = 'sales'
var databricksWarehouseName = take('wh-${environmentName}-${suffix}', 64)
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var aksRbacClusterAdminRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'
)
var networkContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4d97b98b-1d4f-4787-a291-c67834d212e7'
)
var foundryUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)

resource acr 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource ingressPublicIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: ingressPublicIpName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    dnsSettings: {
      domainNameLabel: appDnsLabel
    }
  }
}

var appHost = ingressPublicIp.properties.dnsSettings.fqdn
var appUrl = 'https://${appHost}'

resource aks 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: aksName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  properties: {
    dnsPrefix: take('dns-${environmentName}-${suffix}', 54)
    enableRBAC: true
    disableLocalAccounts: true
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      tenantID: tenant().tenantId
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: 1
        vmSize: nodeVmSize
        mode: 'System'
        osSKU: 'AzureLinux'
        osType: 'Linux'
        type: 'VirtualMachineScaleSets'
      }
    ]
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    ingressProfile: {
      webAppRouting: {
        enabled: false
      }
    }
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, aks.id, acrPullRoleId)
  properties: {
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource ingressNetworkAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aks.id, networkContributorRoleId)
  properties: {
    principalId: aks.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: networkContributorRoleId
  }
}

resource clusterAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aks
  name: guid(aks.id, principalId, aksRbacClusterAdminRoleId)
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: aksRbacClusterAdminRoleId
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: foundryLocation
  identity: {
    type: 'SystemAssigned'
  }
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: foundryProjectName
  location: foundryLocation
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Order Agent'
    description: 'Microsoft Agent Framework client for the FastMCP order server'
  }
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

// There is deliberately no storage account or Access Connector here. Azure
// auto-provisions a Unity Catalog metastore with each new workspace and holds
// the catalog storage behind Databricks Default Storage, so the demo catalog
// needs neither. Customers who must own the storage add an ADLS Gen2 account,
// an Access Connector, and a Unity Catalog storage credential — see the
// customer-managed storage note in the README, which records the two role
// assignments Databricks requires for that path.
//
// Premium is not optional: Unity Catalog, table ACLs, row filters, and column
// masks are all premium-tier features, and the whole point of this workload is
// that Databricks enforces them instead of the MCP server.
//
// Pinned to 2024-05-01 rather than 2026-01-01 on purpose. The newer version
// marks `computeMode` required, and that property decides whether the workspace
// is serverless-only — not something to set blind for a reference deployment.
resource databricks 'Microsoft.Databricks/workspaces@2024-05-01' = if (deployDatabricks) {
  name: databricksWorkspaceName
  location: location
  sku: {
    name: 'premium'
  }
  properties: {
    managedResourceGroupId: subscriptionResourceId(
      'Microsoft.Resources/resourceGroups',
      databricksManagedRgName
    )
    publicNetworkAccess: 'Enabled'
    parameters: {
      enableNoPublicIp: {
        value: true
      }
    }
  }
}

resource clientApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: clientAppUniqueName
  displayName: '${environmentName} Order Agent Chat'
  signInAudience: 'AzureADMyOrg'
  isFallbackPublicClient: false
  publicClient: {
    redirectUris: []
  }
  web: {
    homePageUrl: appUrl
    logoutUrl: '${appUrl}/'
    redirectUris: [
      '${appUrl}/auth/callback'
    ]
    implicitGrantSettings: {
      enableAccessTokenIssuance: false
      enableIdTokenIssuance: false
    }
  }
}

resource serverApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: serverAppUniqueName
  displayName: '${environmentName} FastMCP Orders Server'
  signInAudience: 'AzureADMyOrg'
  api: {
    oauth2PermissionScopes: [
      {
        id: scopeId
        type: 'User'
        adminConsentDescription: 'Call the Order Handling MCP as the signed-in user.'
        adminConsentDisplayName: 'Call the Order Handling MCP'
        userConsentDescription: 'Call the Order Handling MCP as you.'
        userConsentDisplayName: 'Call the Order Handling MCP'
        value: scopeValue
        isEnabled: true
      }
    ]
    preAuthorizedApplications: concat(
      [
        {
          appId: clientApp.appId
          delegatedPermissionIds: [
            scopeId
          ]
        }
      ],
      empty(harnessClientAppId)
        ? []
        : [
            {
              appId: harnessClientAppId
              delegatedPermissionIds: [
                scopeId
              ]
            }
          ]
    )
    requestedAccessTokenVersion: 2
  }
  requiredResourceAccess: concat(
    [
      {
        resourceAppId: graphAppId
        resourceAccess: [
          {
            id: graphUserReadScopeId
            type: 'Scope'
          }
        ]
      }
    ],
    // Delegated only. There is deliberately no application permission here, so
    // this server cannot reach Databricks except while holding a caller's
    // token — the same property the Graph permission above gives it. A test
    // compiles this template and asserts every entry is a 'Scope'.
    !deployDatabricks
      ? []
      : [
          {
            resourceAppId: databricksAppId
            resourceAccess: [
              {
                id: databricksUserImpersonationScopeId
                type: 'Scope'
              }
            ]
          }
        ]
  )
}

resource serverAppUpdate 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: serverAppUniqueName
  displayName: serverApp.displayName
  signInAudience: serverApp.signInAudience
  identifierUris: [
    'api://${serverApp.appId}'
  ]
  api: serverApp.api
  requiredResourceAccess: serverApp.requiredResourceAccess
}

resource clientAppUpdate 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: clientAppUniqueName
  displayName: clientApp.displayName
  signInAudience: clientApp.signInAudience
  isFallbackPublicClient: false
  publicClient: {
    redirectUris: []
  }
  web: clientApp.web
  requiredResourceAccess: [
    {
      resourceAppId: serverApp.appId
      resourceAccess: [
        {
          id: scopeId
          type: 'Scope'
        }
      ]
    }
  ]
}

resource serverServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: serverApp.appId
}

resource clientServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: clientApp.appId
}

resource graphServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' existing = {
  appId: graphAppId
}

resource graphAdminConsent 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = {
  clientId: serverServicePrincipal.id
  consentType: 'AllPrincipals'
  resourceId: graphServicePrincipal.id
  scope: 'User.Read'
}

resource databricksServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' existing = if (deployDatabricks) {
  appId: databricksAppId
}

resource databricksAdminConsent 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = if (deployDatabricks) {
  clientId: serverServicePrincipal.id
  consentType: 'AllPrincipals'
  resourceId: databricksServicePrincipal!.id
  scope: 'user_impersonation'
}

resource mcpAdminConsent 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = {
  clientId: clientServicePrincipal.id
  consentType: 'AllPrincipals'
  resourceId: serverServicePrincipal.id
  scope: scopeValue
}

resource serverWorkloadFederation 'Microsoft.Graph/applications/federatedIdentityCredentials@v1.0' = {
  name: '${serverApp.uniqueName}/aks-workload'
  audiences: [
    'api://AzureADTokenExchange'
  ]
  description: 'AKS service account credential for the FastMCP server'
  issuer: aks.properties.oidcIssuerProfile.issuerURL
  subject: 'system:serviceaccount:${namespaceName}:${serverServiceAccountName}'
}

resource clientWorkloadFederation 'Microsoft.Graph/applications/federatedIdentityCredentials@v1.0' = {
  name: '${clientApp.uniqueName}/aks-workload'
  audiences: [
    'api://AzureADTokenExchange'
  ]
  description: 'AKS service account credential for the Order Agent chat client'
  issuer: aks.properties.oidcIssuerProfile.issuerURL
  subject: 'system:serviceaccount:${namespaceName}:${clientServiceAccountName}'
}

resource clientFoundryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryProject
  name: guid(foundryProject.id, clientAppUniqueName, foundryUserRoleId)
  properties: {
    principalId: clientServicePrincipal.id
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryUserRoleId
  }
}

output AZURE_RESOURCE_GROUP string = resourceGroup().name
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_AKS_CLUSTER_NAME string = aks.name
output AZURE_AKS_NAMESPACE string = namespaceName
output AZURE_CONTAINER_REGISTRY_NAME string = acr.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.properties.loginServer
output AKS_CLUSTER_NAME string = aks.name
output ACR_NAME string = acr.name
output ACR_LOGIN_SERVER string = acr.properties.loginServer
output MCP_IMAGE_NAME string = serverImageName
output CHAT_IMAGE_NAME string = clientImageName
output K8S_NAMESPACE string = namespaceName
output MCP_SERVICE_ACCOUNT string = serverServiceAccountName
output CHAT_SERVICE_ACCOUNT string = clientServiceAccountName
output ENTRA_SERVER_CLIENT_ID string = serverApp.appId
output ENTRA_CLIENT_CLIENT_ID string = clientApp.appId
output MCP_SCOPE string = 'api://${serverApp.appId}/${scopeValue}'
output APP_HOST string = appHost
output APP_URL string = appUrl
output MCP_SERVER_URL string = appUrl
output MCP_ENDPOINT string = '${appUrl}/mcp'
output OAUTH_REDIRECT_URI string = '${appUrl}/auth/callback'
output INGRESS_IP string = ingressPublicIp.properties.ipAddress
output INGRESS_PUBLIC_IP_RESOURCE_GROUP string = resourceGroup().name
output FOUNDRY_PROJECT_ENDPOINT string = foundryProject.properties.endpoints['AI Foundry API']
output FOUNDRY_MODEL_DEPLOYMENT_NAME string = modelDeployment.name
// Empty when deployDatabricks is false, which is exactly what makes the MCP
// server skip registering the Databricks tools.
output DATABRICKS_HOST string = deployDatabricks
  ? 'https://${databricks!.properties.workspaceUrl}'
  : ''
output DATABRICKS_WORKSPACE_NAME string = deployDatabricks ? databricks!.name : ''
output DATABRICKS_WORKSPACE_RESOURCE_ID string = deployDatabricks ? databricks!.id : ''
output DATABRICKS_WORKSPACE_NUMERIC_ID string = deployDatabricks
  ? databricks!.properties.workspaceId
  : ''
// The preferred catalog name. The bootstrap writes back the one it actually
// used, which differs when it falls back to the workspace's managed catalog.
output DATABRICKS_CATALOG string = deployDatabricks ? databricksCatalogName : ''
output DATABRICKS_SCHEMA string = deployDatabricks ? databricksSchemaName : ''
output DATABRICKS_WAREHOUSE_NAME string = deployDatabricks ? databricksWarehouseName : ''
