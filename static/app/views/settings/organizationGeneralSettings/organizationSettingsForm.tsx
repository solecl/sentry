import {Fragment} from 'react';
import {RouteComponentProps} from 'react-router';
import styled from '@emotion/styled';
import {Location} from 'history';
import cloneDeep from 'lodash/cloneDeep';

import {addErrorMessage} from 'sentry/actionCreators/indicator';
import {updateOrganization} from 'sentry/actionCreators/organizations';
import Feature from 'sentry/components/acl/feature';
import FeatureDisabled from 'sentry/components/acl/featureDisabled';
import AsyncComponent from 'sentry/components/asyncComponent';
import AvatarChooser from 'sentry/components/avatarChooser';
import Form from 'sentry/components/forms/form';
import JsonForm from 'sentry/components/forms/jsonForm';
import HookOrDefault from 'sentry/components/hookOrDefault';
import {Hovercard} from 'sentry/components/hovercard';
import Link from 'sentry/components/links/link';
import Tag from 'sentry/components/tag';
import organizationSettingsFields from 'sentry/data/forms/organizationGeneralSettings';
import {IconCodecov, IconLock} from 'sentry/icons';
import {t} from 'sentry/locale';
import {space} from 'sentry/styles/space';
import {IntegrationProvider, Organization, Scope} from 'sentry/types';
import withOrganization from 'sentry/utils/withOrganization';

const HookCodecovSettingsLink = HookOrDefault({
  hookName: 'component:codecov-integration-settings-link',
});

type Props = {
  access: Set<Scope>;
  initialData: Organization;
  location: Location;
  onSave: (previous: Organization, updated: Organization) => void;
  organization: Organization;
} & RouteComponentProps<{}, {}>;

type State = AsyncComponent['state'] & {
  authProvider: object;
  githubIntegration: {providers: IntegrationProvider[]};
};

class OrganizationSettingsForm extends AsyncComponent<Props, State> {
  getEndpoints(): ReturnType<AsyncComponent['getEndpoints']> {
    const {organization} = this.props;
    return [
      ['authProvider', `/organizations/${organization.slug}/auth-provider/`],
      [
        'githubIntegration',
        `/organizations/${organization.slug}/config/integrations/?provider_key=github`,
      ],
    ];
  }

  render() {
    const {initialData, organization, onSave, access} = this.props;
    const {authProvider, githubIntegration} = this.state;
    const endpoint = `/organizations/${organization.slug}/`;
    const hasGithubIntegration = !githubIntegration?.providers[0].canAdd;

    const jsonFormSettings = {
      additionalFieldProps: {hasSsoEnabled: !!authProvider},
      features: new Set(organization.features),
      access,
      location: this.props.location,
      disabled: !access.has('org:write'),
    };

    const forms = cloneDeep(organizationSettingsFields);
    forms[0].fields = [
      ...forms[0].fields,
      {
        name: 'codecovAccess',
        type: 'boolean',
        disabled: !organization.features.includes('codecov-integration'),
        label: (
          <PoweredByCodecov>
            {t('Enable Code Coverage Insights')}{' '}
            <Feature
              hookName="feature-disabled:codecov-integration-setting"
              renderDisabled={p => (
                <Hovercard
                  body={
                    <FeatureDisabled
                      features={p.features}
                      hideHelpToggle
                      featureName={t('Codecov Coverage')}
                    />
                  }
                >
                  <Tag role="status" icon={<IconLock isSolid />}>
                    {t('disabled')}
                  </Tag>
                </Hovercard>
              )}
              features={['organizations:codecov-integration']}
            >
              {() => null}
            </Feature>
          </PoweredByCodecov>
        ),
        formatMessageValue: (value: boolean) => {
          const onOff = value ? t('on') : t('off');
          return t('Codecov access was turned %s', onOff);
        },
        help: (
          <PoweredByCodecov>
            {t('powered by')} <IconCodecov /> Codecov{' '}
            <HookCodecovSettingsLink organization={organization} />
          </PoweredByCodecov>
        ),
      },
      {
        name: 'githubPRBot',
        type: 'boolean',
        label: t('Enable Pull Request Bot'),
        visible: ({features}) => features.has('pr-comment-bot'),
        help: (
          <Fragment>
            {t(
              "Allow Sentry to comment on pull requests about relevant issues impacting your app's performance."
            )}{' '}
            <Link to={`/settings/${organization.slug}/integrations/github`}>
              {t('Configure GitHub integration')}
            </Link>
          </Fragment>
        ),
        disabled: !hasGithubIntegration,
        disabledReason: (
          <Fragment>
            {t('You must have a GitHub integration to enable this feature.')}
          </Fragment>
        ),
      },
    ];

    return (
      <Form
        data-test-id="organization-settings"
        apiMethod="PUT"
        apiEndpoint={endpoint}
        saveOnBlur
        allowUndo
        initialData={initialData}
        onSubmitSuccess={(updated, _model) => {
          // Special case for slug, need to forward to new slug
          if (typeof onSave === 'function') {
            onSave(initialData, updated);
          }
        }}
        onSubmitError={() => addErrorMessage('Unable to save change')}
      >
        <JsonForm {...jsonFormSettings} forms={forms} />
        <AvatarChooser
          type="organization"
          allowGravatar={false}
          endpoint={`${endpoint}avatar/`}
          model={initialData}
          onSave={updateOrganization}
          disabled={!access.has('org:write')}
        />
      </Form>
    );
  }
}

export default withOrganization(OrganizationSettingsForm);

const PoweredByCodecov = styled('div')`
  display: flex;
  align-items: center;
  gap: ${space(0.5)};

  & > span {
    display: flex;
    align-items: center;
  }
`;
