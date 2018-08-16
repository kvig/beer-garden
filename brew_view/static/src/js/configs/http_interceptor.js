
interceptorService.$inject = ['$rootScope', '$templateCache'];
/**
 * interceptorService - Used to intercept API requests.
 * @param  {$rootScope} $rootScope         Angular's $rootScope object.
 * @param  {$templateCache} $templateCache Angular's $templateCache object.
 */
export function interceptorService($rootScope, $templateCache) {
  /* eslint-disable no-invalid-this */
  let service = this;
  service.request = function(config) {
    // Only match things that we know are targeted at our backend
    if ($rootScope.apiBaseUrl && (config.url.startsWith('config') ||
        config.url.startsWith('version') || config.url.startsWith('api'))) {
      config.url = $rootScope.apiBaseUrl + config.url;
    }
    return config;
  };
};

authInterceptorService.$inject = [
  '$q',
  '$injector',
  'localStorageService',
];
/**
 * authInterceptorService - Used to intercept API requests.
 * @param  {$q} $q                                   $q object
 * @param  {$injector} $injector                     $rootScope object
 * @param  {localStorageService} localStorageService Storage service
 * @return {Object}                                  Interceptor object
 */
export function authInterceptorService(
    $q,
    $injector,
    localStorageService) {
  return {
    responseError: (rejection) => {
      // 401 means 'needs authentication'
      if (rejection.status === 401) {
        // This attempts to handle the condition where an access token has
        // expired but there's a refresh token in storage. We use the refresh
        // token to get a new access token then re-attempt the original request.
        let refreshToken = localStorageService.get('refresh');
        if (refreshToken) {
          // Can't use normal dependency injection as it causes a cycle
          let $http = $injector.get('$http');
          let tokenService = $injector.get('TokenService');

          return tokenService.doRefresh(refreshToken).then(
            (response) => {
              tokenService.handleToken(response.data.token);

              // Set the Authorization header to the updated default
              rejection.config.headers.Authorization =
                $http.defaults.headers.common.Authorization;

              // And then retry the original request
              return $http(rejection.config);
            },
            (response) => {
              // Refresh didn't work. Maybe it was expired / removed
              // We're going to retry so clear the bad refresh token so we
              // don't get stuck in an infinite retry cycle
              let $rootScope = $injector.get('$rootScope');
              $rootScope.doLogout();

              // Clear the Authorization header
              rejection.config.headers.Authorization = undefined;

              // And then retry the original request
              return $http(rejection.config);
            }
          );
        } else {
          // Highlight the login button
          $injector.get('$rootScope').loginError = true;
        }
      }

      // We've done all we can, so return the rejection
      return $q.reject(rejection);
    },
  };
};

interceptorConfig.$inject = ['$httpProvider'];
/**
 * interceptorConfig - Angular configuration object for API interceptors.
 * @param  {$httpProvider} $httpProvider Angular's $httpProvider object.
 */
export function interceptorConfig($httpProvider) {
  $httpProvider.interceptors.push('APIInterceptor');
  $httpProvider.interceptors.push('authInterceptorService');
};
